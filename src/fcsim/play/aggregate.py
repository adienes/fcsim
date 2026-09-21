"""`fcsim report`: several games side by side. Every number carries its sample size, because
four games of Commander are a small sample and one is an anecdote (PLAN.md §11.2)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .cardlog import card_record
from .pilotlog import pilot_record
from .rubric import measure
from .status import status


def aggregate(game_dirs: list[str | Path]) -> dict:
    games, decks, cards, unread, pilots = [], {}, {}, [], {}
    for game_dir in game_dirs:
        try:
            scored, seen, piloted = measure(game_dir), card_record(game_dir), pilot_record(game_dir)
        except Exception as error:  # noqa: BLE001 - one journal that does not replay must not cost the report of the others
            unread.append({"directory": Path(game_dir).name, "error": f"{type(error).__name__}: {error}"[:300]})
            continue
        verdicts = {s["measure"]: s["verdict"] for s in scored["scores"]}
        games.append({"directory": Path(scored["directory"]).name, "rounds": scored["rounds"], "over": scored["over"],
                      "winner": scored["winner"], "verdicts": verdicts, "pace": _pace(game_dir)})
        for index, seat in enumerate(scored["seats"]):
            deck = decks.setdefault(seat["name"], {"games": 0, "wins": 0, "turns": 0, "idle_turns": 0, "spells": 0, "damage_to_players": 0,
                                                   "aimed_at_others": 0, "responses": 0, "left_in_rounds": []})
            deck["games"] += 1
            deck["wins"] += scored["winner"] == seat["name"]
            for key in ("turns", "idle_turns", "spells", "damage_to_players", "aimed_at_others", "responses"):
                deck[key] += seat[key]
            if seat["left_in_round"]:
                deck["left_in_rounds"].append(seat["left_in_round"])
            flown, kept = piloted["seats"][index]["totals"], piloted["seats"][index]["kept"] or {}
            hands = pilots.setdefault(seat["name"], {"games": 0, "thin_keeps": 0, "land_drops_missed": 0, "castable_left": 0, "no_attack": 0, "held_back": 0})
            hands["games"] += 1
            hands["thin_keeps"] += len(kept.get("cards", [])) >= 7 and kept.get("lands", 9) + len(kept.get("mana_rocks", [])) <= 2
            for key, measured in (("land_drops_missed", "land_drops_missed_in_first_six"), ("castable_left", "turns_ended_with_a_castable_card_and_the_mana_for_it"),
                                  ("no_attack", "turns_with_attackers_and_no_attack"), ("held_back", "turns_with_attackers_held_back_from_a_player_without_blockers")):
                hands[key] += flown[measured]
            for row in seen["seats"][index]["cards"]:
                card = cards.setdefault(seat["name"], {}).setdefault(row["name"], {"seen": 0, "played": 0, "rounds_held": 0, "stuck_at_end": 0})
                card["seen"] += 1
                card["played"] += row["played_round"] is not None
                card["rounds_held"] += row["rounds_in_hand"]
                card["stuck_at_end"] += row["ended_in"] == "hand" and row["rounds_in_hand"] >= 2
    tally = {measure_: dict(Counter(g["verdicts"].get(measure_, "-") for g in games)) for measure_ in
             ("length", "participation", "interaction", "threat rotation", "turn weight", "ending")}
    return {"games": games, "decks": decks, "cards": cards, "verdicts": tally, "unread": unread, "pilots": pilots}


def _pace(game_dir: str | Path) -> dict:
    """What a game cost in the models' own time, which games played side by side do not share the
    way they share the clock on the wall: a batch's games are compared by this, not by when they ended."""
    told = status(game_dir)
    time, turns = told["time"], max(told["at"]["table_turn"], 1)
    seconds = sum(time[key] for key in ("wake_seconds", "audit_seconds", "refresh_seconds", "ruling_seconds", "triage_seconds", "talk_seconds"))
    return {"table_turns": turns, "model_seconds": seconds, "seconds_per_turn": round(seconds / turns), "wakes_per_turn": round(time["wakes"] / turns, 1),
            "audits": time["audits"], "findings": len(told["findings"]), "rulings": time["rulings"]}


def render(report: dict, *, deck: str | None = None) -> str:
    n = len(report["games"])
    lines = [f"{n} game(s). With so few, read these as observations, not rates.", "",
             "GAMES"] + [f"  {g['directory'][-5:]}  {g['directory'][:-5][:56]:<56}  {g['rounds']:>2} rounds  " + (f"won by {g['winner']}" if g["winner"] else "unfinished")
                         for g in report["games"]]
    lines += [f"  {g['directory'][-5:]}  COULD NOT BE READ and is in none of the numbers below: {g['error']}" for g in report.get("unread", [])]
    lines += ["", "PACE    in the models' own seconds, so games played side by side compare",
              "  game   table turns  seconds a turn  wakes a turn  audits  findings  rulings"]
    lines += [f"  {g['directory'][-5:]:<7}{p['table_turns']:>11}{p['seconds_per_turn']:>16}{p['wakes_per_turn']:>14}{p['audits']:>8}{p['findings']:>10}{p['rulings']:>9}"
              for g in report["games"] for p in [g["pace"]]]
    lines += ["", "RUBRIC  how many games got each verdict"]
    lines += [f"  {measure_:<17}" + "  ".join(f"{verdict} {count}" for verdict, count in sorted(row.items())) for measure_, row in report["verdicts"].items()]
    lines += ["", "DECKS   games  wins  turns  idle  spells  damage to players  aimed at others  at instant speed  left in round"]
    lines += [f"  {name:<18}{d['games']:>3}{d['wins']:>6}{d['turns']:>7}{d['idle_turns']:>6}{d['spells']:>8}{d['damage_to_players']:>19}"
              f"{d['aimed_at_others']:>17}{d['responses']:>18}  {', '.join(map(str, d['left_in_rounds'])) or '-'}" for name, d in report["decks"].items()]
    lines += ["", "PILOTS  what was played, not what was in the list: sevens kept on two mana sources or fewer; land drops missed in the first six turns;",
              "        turns ended with a castable card and the mana for it; turns with attackers and no attack; turns attackers stayed home from an open player",
              "  deck               games  thin keeps  land drops missed  castable left  no attack  held back"]
    lines += [f"  {name:<18}{p['games']:>6}{p['thin_keeps']:>12}{p['land_drops_missed']:>19}{p['castable_left']:>15}{p['no_attack']:>11}{p['held_back']:>11}"
              for name, p in report.get("pilots", {}).items()]
    for name, rows in report["cards"].items():
        if deck is None or deck.casefold() not in name.casefold():
            continue
        lines += ["", f"CARDS {name} saw  (seen in n games, played, rounds held in total, still in hand at the end after two rounds or more)"]
        lines += [f"  {card:<36}seen {c['seen']}  played {c['played']}  held {c['rounds_held']:>2}" + ("  STUCK" * (c["stuck_at_end"] > 0))
                  for card, c in sorted(rows.items(), key=lambda kv: (-kv[1]["stuck_at_end"], -kv[1]["rounds_held"], kv[0]))]
    return "\n".join(lines)
