"""Score a game against the table-quality rubric (PLAN.md §12.1), from its journal alone.

Only what a card-blind log can show is measured here: how long the game ran, who made plays, who
sat out, how much the players did to each other, whom the table leaned on, how heavy the turns
were and how the losers went out. Whether each deck got to do its thing is for a reader to judge;
these numbers are what that judgement should start from. The thresholds are stated with each score.
"""

from __future__ import annotations

from pathlib import Path
from statistics import median

from ..table import Table
from .gamedir import GameDirectory

_STARTING_LIFE = 40  # Commander
_PLAYS = ("cast", "activate", "add_trigger", "resolve", "act", "play_land", "declare_attackers", "declare_blockers", "combat_damage")


def measure(game_dir: str | Path) -> dict:
    directory = GameDirectory.open(game_dir)
    config = directory.read("config.json")
    names = config.get("seats") or [seat["name"] for seat in config["table"]["seats"]]
    count = len(names)
    turns: dict[int, dict] = {}       # table turn -> what its active player did with it
    life_at_start: dict[int, list[int]] = {}
    seats = [{"name": name, "spells": 0, "attackers": 0, "blockers": 0, "damage_to_players": 0, "aimed_at_others": 0,
              "aimed_at_by_others": 0, "attacked_by_others": 0, "responses": 0} for name in names]
    pressure: dict[int, list[int]] = {}  # round -> per seat, the attackers and targeted effects pointed at it
    out: dict[int, dict] = {}
    last = {"turn": 0, "round": 0}

    def before(table: Table, event: dict) -> None:
        game = table.game
        for seat in game.seats:  # whoever has left since the last event left during it
            if seat.status != "alive" and seat.index not in out:
                out[seat.index] = {"turn": last["turn"], "round": last["round"], "life_at_start_of_turn": life_at_start.get(last["turn"], [None] * count)[seat.index],
                                   "conceded": seat.status == "conceded"}
        if game.control.kind == "setup":
            return
        active, number = game.turn.active, game.turn.number
        this_round = game.seats[active].turns_taken
        last.update(turn=number, round=this_round)
        life_at_start.setdefault(number, [s.life for s in game.seats])
        turn = turns.setdefault(number, {"seat": active, "round": this_round, "land": False, "spells": 0, "abilities": 0,
                                         "attackers": 0, "actions": 0})
        seat, tool, args = event["seat"], event["tool"], event["args"]
        if seat is None:
            return
        if seat == active and tool in _PLAYS:
            turn["actions"] += 1

        def owner(target: dict) -> int | None:
            if "seat" in target:
                return target["seat"]
            if "item" in target:
                return next((i.controller for i in game.stack if i.id == target["item"]), None)
            return game.objects[target["obj"]].controller if target.get("obj") in game.objects else None

        if tool in ("cast", "activate", "add_trigger") and not args.get("mana_ability"):
            aimed = {owner(t) for t in args.get("targets", [])} - {seat, None}
            for other in aimed:
                seats[seat]["aimed_at_others"] += 1
                seats[other]["aimed_at_by_others"] += 1
                pressure.setdefault(this_round, [0] * count)[other] += 1
            if tool != "add_trigger" and (seat != active or (game.stack and game.stack[-1].controller != seat)):
                seats[seat]["responses"] += 1  # at instant speed: on another's turn, or in answer to another's spell
            if tool == "cast":
                seats[seat]["spells"] += 1
            if seat == active and tool != "add_trigger":
                turn["spells" if tool == "cast" else "abilities"] += 1
        elif tool == "play_land" and seat == active:
            turn["land"] = True
        elif tool == "declare_attackers":
            turn["attackers"] += len(args.get("attacks", []))
            seats[seat]["attackers"] += len(args.get("attacks", []))
            for attack in args.get("attacks", []):
                defender = owner(attack["target"])
                if defender is not None and defender != seat:
                    seats[defender]["attacked_by_others"] += 1
                    pressure.setdefault(this_round, [0] * count)[defender] += 1
        elif tool == "declare_blockers":
            seats[seat]["blockers"] += len(args.get("blocks", []))
        elif tool == "combat_damage":
            for hit in args.get("assignments", []):
                source = game.objects.get(hit.get("source"))
                if "seat" in hit.get("target", {}) and source is not None and source.controller != hit["target"]["seat"]:
                    seats[source.controller]["damage_to_players"] += int(hit.get("amount") or 0)

    table = Table.replay(config["table"], directory.events(), before=before)
    game = table.game
    before(table, {"seat": None, "tool": "end", "args": {}})  # whoever left during the last event
    over = game.control.kind == "over"
    rounds = max((s.turns_taken for s in game.seats), default=0)
    for index, row in enumerate(seats):
        own = [t for t in turns.values() if t["seat"] == index]
        settled = [t for t in own if t["round"] >= 3]  # a land and a pass is a normal first or second turn
        row.update(turns=len(own), missed_land_drops_in_first_five=sum(1 for t in own if t["round"] <= 5 and not t["land"]),
                   idle_turns=sum(1 for t in settled if not (t["spells"] or t["abilities"] or t["attackers"])), turns_from_third=len(settled),
                   status=game.seats[index].status, left_in_round=out.get(index, {}).get("round"),
                   rounds_sat_out=rounds - out[index]["round"] if index in out else 0)
    return {"directory": str(directory.path), "seats": seats, "rounds": rounds, "over": over,
            "winner": names[game.winner] if game.winner is not None else None,
            "scores": _scores(names, seats, turns, pressure, out, rounds, over, _STARTING_LIFE)}


def _scores(names: list[str], seats: list[dict], turns: dict, pressure: dict, out: dict, rounds: int, over: bool, starting_life: int) -> list[dict]:
    scores = []

    def score(measure: str, verdict: str, finding: str, rule: str) -> None:
        scores.append({"measure": measure, "verdict": verdict, "finding": finding, "rule": rule})

    score("length", ("good" if 4 <= rounds <= 10 else "bad") if over else "unfinished",
          f"{rounds} rounds" + ("" if over else " so far; the game was not played to its end"), "good: over in 4 to 10 rounds")

    idle = [(s["name"], s["idle_turns"], s["turns_from_third"]) for s in seats if s["turns_from_third"] >= 2]
    worst = max((n / of for _, n, of in idle), default=0)
    score("participation", "bad" if worst >= 0.5 else "mixed" if worst >= 1 / 3 else "good",
          "; ".join(f"{name} did nothing on {n} of {of} turns" for name, n, of in idle if n) or "every seat made a play on every turn from its third",
          "a turn from a seat's third onward with no spell, no ability and no attack; bad: half a seat's turns, mixed: a third")

    waits = [(names[i], rounds - o["round"]) for i, o in out.items() if over]
    longest = max((w for _, w in waits), default=0)
    score("sitting out", "bad" if longest > 2 else "mixed" if longest > 1 else "good",
          "; ".join(f"{name} was out for {w} round{'s' * (w != 1)} before the end" for name, w in waits if w) or "nobody was out for a whole round before the end",
          "rounds between a seat's elimination and the end; good: at most 1, bad: more than 2")

    acts = sum(s["aimed_at_others"] + s["blockers"] for s in seats)
    replies = sum(s["responses"] for s in seats)
    rate = acts / rounds if rounds else 0
    score("interaction", "good" if rate >= 2 else "mixed" if rate >= 1 else "bad",
          f"{acts} spells, abilities and blockers pointed at another player's things ({rate:.1f} a round); {replies} plays at instant speed "
          "on another's turn or in answer to a spell", "targeted effects at others plus blockers, per round; good: 2 or more, bad: under 1")

    leaned_on = [max(range(len(names)), key=lambda i: row[i]) for _, row in sorted(pressure.items()) if any(row)]
    share = max((leaned_on.count(i) for i in set(leaned_on)), default=0) / len(leaned_on) if leaned_on else 0
    score("threat rotation", "bad" if not leaned_on else "good" if len(set(leaned_on)) >= 2 and share <= 0.6 else "mixed",
          ("by round, the table leaned hardest on: " + ", ".join(names[i] for i in leaned_on)) if leaned_on else "nobody was attacked or targeted all game",
          "the seat most attacked and targeted each round; good: at least two seats, none of them in over 60% of rounds")

    weights = [t["actions"] for t in turns.values() if t["actions"]]
    typical = median(weights) if weights else 0
    number, heaviest = max(turns.items(), key=lambda kv: kv[1]["actions"], default=(0, {"actions": 0, "seat": 0}))
    ratio = heaviest["actions"] / typical if typical else 0
    score("turn weight", "bad" if ratio >= 8 else "mixed" if ratio >= 4 else "good",
          f"the heaviest turn was {names[heaviest['seat']]}'s table turn {number}: {heaviest['actions']} actions against a typical {typical:g}",
          "the longest turn, in logged actions, against the median turn; good: under 4 times, bad: 8 times or more")

    if over:
        sudden = [(names[i], o["life_at_start_of_turn"]) for i, o in out.items()
                  if o["life_at_start_of_turn"] is not None and o["life_at_start_of_turn"] >= starting_life / 2 and not o["conceded"]]
        conceded = [names[i] for i, o in out.items() if o["conceded"]]
        score("ending", "bad" if conceded or (out and len(sudden) == len(out)) else "mixed" if sudden else "good",
              "; ".join([f"{name} went out in one turn from {life} life" for name, life in sudden] + [f"{name} conceded" for name in conceded])
              or "every player who lost was already under half their starting life when the turn that ended them began",
              "life at the start of the turn a seat was eliminated; from half its starting life or more reads as a win from nowhere")
    quiet = [s["name"] for s in seats if not s["damage_to_players"] and not s["aimed_at_by_others"] and not s["attacked_by_others"]]
    score("spotlight", "mixed" if quiet else "for a reader",
          ("dealt no damage to a player and was never attacked or targeted: " + ", ".join(quiet)) if quiet
          else "every seat dealt damage or drew the table's attention; whether each deck did its thing is for a reader to judge",
          "a seat that neither hurt anyone nor was ever worth attacking or targeting probably never mattered")
    return scores


def render_markdown(result: dict) -> str:
    """The same scores as a section of a game's report."""
    lines = ["## Against the rubric", "", "Measured from the log; the thresholds are in `rubric.json`. Whether each deck got to do its "
             "thing is for a reader to judge.", "", "| Measure | Verdict | What the log shows |", "| --- | --- | --- |"]
    lines += [f"| {s['measure']} | {s['verdict']} | {s['finding']} |" for s in result["scores"]]
    lines += ["", "| Seat | Turns | Idle turns | Missed land drops (first 5) | Spells | Attackers | Blockers | Damage to players | "
              "Aimed at others | Aimed at or attacked by others | Left in round |", "| --- " * 11 + "|"]
    lines += [f"| {s['name']} | {s['turns']} | {s['idle_turns']} | {s['missed_land_drops_in_first_five']} | {s['spells']} | {s['attackers']} | "
              f"{s['blockers']} | {s['damage_to_players']} | {s['aimed_at_others']} | {s['aimed_at_by_others'] + s['attacked_by_others']} | "
              f"{s['left_in_round'] or '-'} |" for s in result["seats"]]
    return "\n".join(lines) + "\n"


def render(result: dict) -> str:
    lines = [f"RUBRIC  {result['rounds']} rounds" + (f", won by {result['winner']}" if result["winner"] else "" if result["over"] else ", unfinished")]
    lines += [f"  {s['verdict']:<14}{s['measure']:<17}{s['finding']}" for s in result["scores"]]
    lines += ["", "  seat               turns  idle  no land (first 5)  spells  attackers  blockers  damage to players  aimed at others / by others  left in round"]
    lines += [f"  {s['name']:<18}{s['turns']:>6}{s['idle_turns']:>6}{s['missed_land_drops_in_first_five']:>19}{s['spells']:>8}{s['attackers']:>11}"
              f"{s['blockers']:>10}{s['damage_to_players']:>19}{s['aimed_at_others']:>17} / {s['aimed_at_by_others'] + s['attacked_by_others']:<9}"
              f"{s['left_in_round'] or '-':>14}" for s in result["seats"]]
    return "\n".join(lines)
