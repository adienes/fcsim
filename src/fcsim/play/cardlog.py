"""What happened to each card a seat saw in one game: when it was drawn, when it was played, how
long it sat in hand, where it ended. Full information, read from the journal; nothing here is
shown to players. It is the raw material for judging a list from its games (PLAN.md §12.3).
"""

from __future__ import annotations

from pathlib import Path

from ..table import Table
from .gamedir import GameDirectory


def card_record(game_dir: str | Path) -> dict:
    """Per seat, one row per physical card that left the library or the command zone."""
    directory = GameDirectory.open(game_dir)
    config = directory.read("config.json")
    names = config.get("seats") or [seat["name"] for seat in config["table"]["seats"]]
    cards: dict[int, dict] = {}   # by uid, which a card keeps as it changes zones
    where: dict[int, str] = {}
    opened = [False]

    def look(table: Table, event: dict | None = None) -> None:
        game = table.game
        if game.control.kind == "setup":
            return  # hands before the keep are mulligan fodder, not draws
        this_round = game.seats[game.turn.active].turns_taken
        opening, opened[0] = not opened[0], True  # the first look after the keeps: what is in hand is the opening hand
        for obj in game.objects.values():
            if obj.token is not None or (obj.zone in ("library", "graveyard") and obj.uid not in cards):
                continue  # still unseen, or milled: says nothing about the card
            before = where.get(obj.uid, "command" if obj.is_commander else "library")
            row = cards.setdefault(obj.uid, {"seat": obj.owner, "name": obj.name, "commander": obj.is_commander, "seen_round": None,
                                             "played_round": None, "played_as": None, "times_cast": 0, "rounds_in_hand": 0, "_since": None,
                                             "seen_in": None})
            if obj.zone != before:
                if obj.zone in ("hand", "exile") and row["seen_round"] is None and (obj.zone == "hand" or before == "library"):
                    # Exiled face up from the library is how an "exile the top card, you may play it" deck sees cards.
                    row["seen_round"], row["seen_in"] = (0 if opening else this_round), obj.zone
                if obj.zone == "hand":
                    row["_since"] = this_round
                if before == "hand" and row["_since"] is not None:
                    row["rounds_in_hand"] += this_round - row["_since"]
                    row["_since"] = None
                if obj.zone in ("stack", "battlefield") and before in ("hand", "command", "exile"):
                    row["times_cast"] += obj.zone == "stack"
                    if row["played_round"] is None:
                        row["played_round"], row["played_as"] = this_round, "spell" if obj.zone == "stack" else "land or put into play"
                        row["played_from"] = before
                where[obj.uid] = obj.zone
            row["ended_in"] = obj.zone

    table = Table.replay(config["table"], directory.events(), card_lookup=directory.card_lookup(), before=look)
    look(table)
    # As the rubric counts: a capped game stops as a seat begins the round past the cap, and that turn, with nothing played in it,
    # is no round taken.
    game = table.game
    unplayed = game.control.kind != "over" and not any(e["turn"] == game.turn.number and e["seat"] == game.turn.active
                                                       and e["tool"] not in ("pass_priority", "set_response_policy", "write_notes")
                                                       for e in directory.events()[-400:])
    last_round = max((s.turns_taken - (1 if unplayed and s.index == game.turn.active else 0) for s in game.seats), default=0)
    seats = [{"name": name, "cards": []} for name in names]
    for row in cards.values():
        if row["_since"] is not None:  # still in hand when the game stopped
            row["rounds_in_hand"] += (table.game.seats[row["seat"]].turns_taken or last_round) - row["_since"]
        if row["seen_round"] is None and not row["commander"]:
            continue
        seats[row["seat"]]["cards"].append({k: v for k, v in row.items() if not k.startswith("_") and k != "seat"})
    for seat in seats:
        seat["cards"].sort(key=lambda r: (r["seen_round"] is None, r["seen_round"] or 0, r["name"]))
    return {"directory": str(directory.path), "rounds": last_round, "seats": seats}


def render(record: dict, seat: int) -> str:
    rows = record["seats"][seat]["cards"]
    lines = [f"{record['seats'][seat]['name']}: cards seen in {record['rounds']} rounds (round 0 is the opening hand)",
             "  seen  played  held  ended in     card"]
    lines += [f"  {'-' if r['seen_round'] is None else r['seen_round']:>4}  {'-' if r['played_round'] is None else r['played_round']:>6}"
              f"  {r['rounds_in_hand']:>4}  {r['ended_in']:<11}  {r['name']}"
              + (" (exiled from the library" + (", played from exile)" if r.get("played_from") == "exile" else ", never played)") if r["seen_in"] == "exile" else "")
              + (" (commander" + (f", cast {r['times_cast']}x)" if r["times_cast"] else ", never cast)")
                                                                               if r["commander"] else "") for r in rows]
    return "\n".join(lines)
