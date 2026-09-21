"""How each seat was piloted, measured from the journal.

A deck that loses has not shown that it is weak: it may have been played badly. The rubric says how
a game went and the card record what became of each card; neither says whether the pilot kept a
hand it should have sent back, left mana unused beside cards it could have cast, or held creatures
back from a player with no blockers. Those are facts the journal holds, and nobody has to guess at
them: what a seat kept, and on each of its own turns what mana it had, what it left untapped, what
it could still have cast, and what could have attacked against what stood in the way.

They are told, not judged. Holding up an instant is a decision, and so is not attacking into four
first strikers: the record gives the numbers, and the lesson learner and the reviewer weigh them.
"""

from __future__ import annotations

from pathlib import Path

from ..table import Table, mana, mechanics
from .gamedir import GameDirectory


def pilot_record(game_dir: str | Path) -> dict:
    directory = GameDirectory.open(game_dir)
    config = directory.read("config.json")
    names = config.get("seats") or [seat["name"] for seat in config["table"]["seats"]]
    lookup = directory.card_lookup()
    seats = [{"name": name, "kept": None, "turns": [], "left": None} for name in names]
    now: dict = {"turn": None, "row": None, "seat": None}

    def told(name: str) -> dict:
        return (lookup(name) if lookup else None) or {}

    def land(obj) -> bool:
        return "land" in obj.type_line.lower() or "Land" in told(obj.name).get("types", [])

    def creature(obj) -> bool:
        return "creature" in obj.type_line.lower()

    def close(table: Table) -> None:
        """The turn is over and nothing of this seat's has untapped since: what it left unused."""
        game, seat, row = table.game, now["seat"], now["row"]
        if game.seats[seat].status != "alive":
            return
        mine = [game.objects[i] for i in game.battlefield if game.objects[i].controller == seat]
        row["untapped_sources"] = sum(1 for obj in mine if not obj.tapped and mana.source(game, obj, lookup))
        castable = [entry["name"] for entry in table.affordable(seat)]
        quick = [name for name in castable if "Instant" in told(name).get("types", []) or "Flash" in told(name).get("keywords", [])]
        row["castable_left"], row["castable_left_at_instant_speed"] = [name for name in castable if name not in quick], quick
        row["hand_at_end"] = len(game.seats[seat].hand)
        row["creatures_left_untapped"] = sum(1 for obj in mine if creature(obj) and not obj.tapped)  # what stands between it and three opponents
        row["life_at_end"] = game.seats[seat].life

    def before(table: Table, event: dict) -> None:
        game = table.game
        if event["tool"] == "rewind":
            return  # the table still stands where the abandoned line left it: nothing to read until it has gone back
        if event["tool"] == "keep" and event["seat"] is not None:
            bottomed = set(event["args"].get("bottom") or [])
            hand = [game.objects[i] for i in game.seats[event["seat"]].hand if i not in bottomed]
            spells = [obj for obj in hand if not land(obj)]
            seats[event["seat"]]["kept"] = {
                "mulligans": game.seats[event["seat"]].mulligans, "cards": [obj.name for obj in hand], "lands": len(hand) - len(spells),
                "mana_rocks": [obj.name for obj in spells if told(obj.name).get("produces") and "Artifact" in told(obj.name).get("types", [])],
                "plays_costing_two_or_less": [obj.name for obj in spells if told(obj.name).get("mana_value", 99) <= 2]}
        for seat in game.seats:
            if seat.status != "alive" and seats[seat.index]["left"] is None and game.turn.number >= 1:
                seats[seat.index]["left"] = {"turn": game.turn.number, "round": max(seat.turns_taken, 1), "on_the_turn_of": names[game.turn.active]}
        if game.turn.number != now["turn"]:
            if now["row"] is not None:
                close(table)
            now["turn"], now["seat"], now["row"] = game.turn.number, game.turn.active, None
            me = game.seats[game.turn.active]
            again = next((t for t in seats[me.index]["turns"] if t["turn"] == game.turn.number), None)
            if again is not None:
                now["row"] = again  # a turn the game was rewound into goes on where its record stood
            elif game.turn.number >= 1 and me.status == "alive":
                mine = [game.objects[i] for i in game.battlefield if game.objects[i].controller == me.index]
                now["row"] = {"turn": game.turn.number, "round": me.turns_taken, "life": me.life, "hand": len(me.hand),
                              "lands": sum(1 for obj in mine if land(obj)),
                              "other_sources": sum(1 for obj in mine if not land(obj) and mana.source(game, obj, lookup)),
                              "played_land": False, "cast": [], "could_attack": None, "attacked_with": 0, "untapped_blockers": {}}
                seats[me.index]["turns"].append(now["row"])
        row, seat = now["row"], now["seat"]
        if row is None:
            return
        if game.turn.step in ("begin_combat", "declare_attackers") and row["could_attack"] is None:
            mine = [game.objects[i] for i in game.battlefield if game.objects[i].controller == seat and creature(game.objects[i])]
            row["could_attack"] = [obj.name for obj in mine if mechanics.attack_problem(game, obj, lookup) is None]
            row["untapped_blockers"] = {names[other.index]: sum(1 for i in game.battlefield if game.objects[i].controller == other.index
                                                                and creature(game.objects[i]) and not game.objects[i].tapped)
                                        for other in game.seats if other.index != seat and other.status == "alive"}
        if event["seat"] == seat and event["tool"] == "play_land":
            row["played_land"] = True
        elif event["seat"] == seat and event["tool"] == "cast" and event["args"].get("card") in game.objects:
            row["cast"].append(game.objects[event["args"]["card"]].name)
        elif event["seat"] == seat and event["tool"] == "declare_attackers":
            row["attacked_with"] += len(event["args"].get("attacks", []))

    table = Table.replay(config["table"], directory.events(), card_lookup=lookup, before=before)
    for seat in table.game.seats:  # whoever left during the last event
        if seat.status != "alive" and seats[seat.index]["left"] is None:
            seats[seat.index]["left"] = {"turn": table.game.turn.number, "round": max(seat.turns_taken, 1), "on_the_turn_of": names[table.game.turn.active]}
    for index, seat in enumerate(seats):
        turns = seat["turns"]
        for turn, later in zip(turns, [*turns[1:], None], strict=True):  # what the table did to it before it next untapped
            after = later["life"] if later else table.game.seats[index].life
            if "life_at_end" in turn:
                turn["life_lost_before_next_turn"] = max(turn["life_at_end"] - after, 0)
        seat["totals"] = {
            "turns": len(turns),
            "land_drops_missed_in_first_six": sum(1 for t in turns if t["round"] <= 6 and not t["played_land"]),
            # Mana left open beside an instant is how interaction is held up; with nothing at instant speed in hand it is mana unspent.
            "turns_ended_with_a_castable_card_and_the_mana_for_it": sum(1 for t in turns if t.get("castable_left") and not t.get("castable_left_at_instant_speed")),
            "turns_ended_holding_mana_for_an_instant": sum(1 for t in turns if t.get("castable_left_at_instant_speed") and t.get("untapped_sources")),
            "turns_with_attackers_held_back_from_a_player_without_blockers": sum(
                1 for t in turns if len(t["could_attack"] or []) > t["attacked_with"] and any(n == 0 for n in t["untapped_blockers"].values())),
            "turns_with_attackers_and_no_attack": sum(1 for t in turns if (t["could_attack"] or []) and not t["attacked_with"]),
            "turns_ended_with_creatures_but_none_untapped": sum(1 for t in turns if t["attacked_with"] and t.get("creatures_left_untapped") == 0),
            "life_lost_after_such_turns": sum(t.get("life_lost_before_next_turn", 0) for t in turns if t["attacked_with"] and t.get("creatures_left_untapped") == 0)}
    return {"directory": str(directory.path), "seats": seats}


def render_pilot(record: dict, seat: int) -> str:
    """One seat's piloting, as the numbers have it."""
    row = record["seats"][seat]
    kept, totals = row["kept"], row["totals"]
    lines = [f"How {row['name']} was piloted (computed from the journal; told, not judged)"]
    if kept:
        lines.append(f"  opening hand kept after {kept['mulligans']} mulligan(s): {kept['lands']} land(s), {len(kept['mana_rocks'])} mana rock(s), "
                     f"{len(kept['plays_costing_two_or_less'])} play(s) costing two or less: {', '.join(kept['cards'])}")
    for t in row["turns"]:
        blockers = ", ".join(f"{name} {n}" for name, n in t["untapped_blockers"].items())
        lines.append(f"  round {t['round']} (table turn {t['turn']}): {t['life']} life, {t['hand']} in hand, {t['lands']} land(s) and {t['other_sources']} other mana "
                     f"source(s) as it began; {'played a land' if t['played_land'] else 'NO LAND PLAYED'}; cast {', '.join(t['cast']) or 'nothing'}; "
                     f"could attack with {len(t['could_attack'] or [])}, attacked with {t['attacked_with']}" + (f" (untapped blockers: {blockers})" if t["could_attack"] else "")
                     + (f"; left {t['creatures_left_untapped']} creature(s) untapped and lost {t.get('life_lost_before_next_turn', 0)} life before its next turn"
                        if "creatures_left_untapped" in t else "")
                     + f"; ended with {t.get('untapped_sources', '?')} mana source(s) untapped"
                     + (f" and could still have cast {', '.join(t['castable_left'])}" if t.get("castable_left") else "")
                     + (f" (held at instant speed: {', '.join(t['castable_left_at_instant_speed'])})" if t.get("castable_left_at_instant_speed") else ""))
    if row["left"]:
        lines.append(f"  left the game in round {row['left']['round']} (table turn {row['left']['turn']}, {row['left']['on_the_turn_of']}'s turn)")
    lines.append(f"  in all: {totals['turns']} turns; {totals['land_drops_missed_in_first_six']} land drop(s) missed in the first six; "
                 f"{totals['turns_ended_with_a_castable_card_and_the_mana_for_it']} turn(s) ended with a castable card, the mana for it and no instant to hold it for "
                 f"({totals['turns_ended_holding_mana_for_an_instant']} more with mana open beside an instant); "
                 f"{totals['turns_with_attackers_and_no_attack']} turn(s) with attackers and no attack; "
                 f"{totals['turns_with_attackers_held_back_from_a_player_without_blockers']} turn(s) when attackers stayed home though some player had no untapped blocker; "
                 f"{totals['turns_ended_with_creatures_but_none_untapped']} turn(s) it attacked and left no creature untapped, losing {totals['life_lost_after_such_turns']} life "
                 "in all before its next turns")
    return "\n".join(lines)
