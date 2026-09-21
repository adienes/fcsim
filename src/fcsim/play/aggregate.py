"""`fcsim report`: several games side by side. Every number carries its sample size, because
four games of Commander are a small sample and one is an anecdote (PLAN.md §11.2)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .cardlog import card_record
from .rubric import measure


def aggregate(game_dirs: list[str | Path]) -> dict:
    games, decks, cards = [], {}, {}
    for game_dir in game_dirs:
        scored, seen = measure(game_dir), card_record(game_dir)
        verdicts = {s["measure"]: s["verdict"] for s in scored["scores"]}
        games.append({"directory": Path(scored["directory"]).name, "rounds": scored["rounds"], "over": scored["over"],
                      "winner": scored["winner"], "verdicts": verdicts})
        for index, seat in enumerate(scored["seats"]):
            deck = decks.setdefault(seat["name"], {"games": 0, "wins": 0, "turns": 0, "idle_turns": 0, "spells": 0, "damage_to_players": 0,
                                                   "aimed_at_others": 0, "responses": 0, "left_in_rounds": []})
            deck["games"] += 1
            deck["wins"] += scored["winner"] == seat["name"]
            for key in ("turns", "idle_turns", "spells", "damage_to_players", "aimed_at_others", "responses"):
                deck[key] += seat[key]
            if seat["left_in_round"]:
                deck["left_in_rounds"].append(seat["left_in_round"])
            for row in seen["seats"][index]["cards"]:
                card = cards.setdefault(seat["name"], {}).setdefault(row["name"], {"seen": 0, "played": 0, "rounds_held": 0, "stuck_at_end": 0})
                card["seen"] += 1
                card["played"] += row["played_round"] is not None
                card["rounds_held"] += row["rounds_in_hand"]
                card["stuck_at_end"] += row["ended_in"] == "hand" and row["rounds_in_hand"] >= 2
    tally = {measure_: dict(Counter(g["verdicts"].get(measure_, "-") for g in games)) for measure_ in
             ("length", "participation", "sitting out", "interaction", "threat rotation", "turn weight", "ending")}
    return {"games": games, "decks": decks, "cards": cards, "verdicts": tally}


def render(report: dict, *, deck: str | None = None) -> str:
    n = len(report["games"])
    lines = [f"{n} game(s). With so few, read these as observations, not rates.", "",
             "GAMES"] + [f"  {g['directory'][:60]:<60}  {g['rounds']:>2} rounds  " + (f"won by {g['winner']}" if g["winner"] else "unfinished")
                         for g in report["games"]]
    lines += ["", "RUBRIC  how many games got each verdict"]
    lines += [f"  {measure_:<17}" + "  ".join(f"{verdict} {count}" for verdict, count in sorted(row.items())) for measure_, row in report["verdicts"].items()]
    lines += ["", "DECKS   games  wins  turns  idle  spells  damage to players  aimed at others  at instant speed  left in round"]
    lines += [f"  {name:<18}{d['games']:>3}{d['wins']:>6}{d['turns']:>7}{d['idle_turns']:>6}{d['spells']:>8}{d['damage_to_players']:>19}"
              f"{d['aimed_at_others']:>17}{d['responses']:>18}  {', '.join(map(str, d['left_in_rounds'])) or '-'}" for name, d in report["decks"].items()]
    for name, rows in report["cards"].items():
        if deck is None or deck.casefold() not in name.casefold():
            continue
        lines += ["", f"CARDS {name} saw  (seen in n games, played, rounds held in total, still in hand at the end after two rounds or more)"]
        lines += [f"  {card:<36}seen {c['seen']}  played {c['played']}  held {c['rounds_held']:>2}" + ("  STUCK" * (c["stuck_at_end"] > 0))
                  for card, c in sorted(rows.items(), key=lambda kv: (-kv[1]["stuck_at_end"], -kv[1]["rounds_held"], kv[0]))]
    return "\n".join(lines)
