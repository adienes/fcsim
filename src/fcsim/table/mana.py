"""Whether the permanents a player taps can pay a mana cost, and what a player could afford.

The table is told each card's mana cost, the colors of mana it can make and how much (from the
card data, not from reading the card). With that it can refuse {G}{G} paid with one green source,
which an auditor otherwise finds a turn later, and it can tell a player what is castable so that
no model counts lands. Cost reducers, extra costs and mana from anything but a tapped permanent
are beyond it: a payment with a `note` is left to the auditor, as before.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .model import Game, Obj, TableError

COLORS = "WUBRG"
_SYMBOL = re.compile(r"\{([^}]+)\}")


def parse_cost(cost: str, x: int = 0) -> tuple[int, list[set[str]]] | None:
    """(generic mana, one set of acceptable colors per colored symbol), or None for a cost the
    table does not understand. Hybrid accepts either half; Phyrexian mana may be life, so it asks nothing."""
    generic, colored = 0, []
    for symbol in _SYMBOL.findall(cost or ""):
        parts = symbol.upper().split("/")
        if symbol.isdigit():
            generic += int(symbol)
        elif symbol.upper() == "X":
            generic += x
        elif "P" in parts:
            continue
        elif symbol.upper() in ("C", "S"):
            colored.append({symbol.upper()})
        elif all(part in COLORS or part.isdigit() for part in parts):
            wanted = {part for part in parts if part in COLORS}
            if any(part.isdigit() for part in parts):  # {2/W}: two generic or one white
                generic += 0 if wanted else int(parts[0])
            colored.append(wanted)
        else:
            return None
    return generic, colored


LIFETIMES = ("step", "end_of_combat", "end_of_turn", "kept")
_SOONEST = ("step", "end_of_combat", "end_of_turn", "held", "kept")  # "held" is the table's own: see `empty_pools`


def parse_mana(text) -> list[str] | None:
    """Mana written as "RRG", "{R}{R}{G}" or "2" (that much colorless): one symbol each, or None if it is not mana."""
    found = []
    for symbol in (_SYMBOL.findall(str(text)) or list(str(text).replace(" ", ""))):
        if symbol.isdigit():
            found += ["C"] * int(symbol)
        elif symbol.upper() in COLORS + "C":
            found.append(symbol.upper())
        else:
            return None
    return found or None


def _keeper(game: Game, seat: int) -> str | None:
    """The name of a permanent of this player whose continuous effect, as the ledger has it, speaks of unspent mana."""
    for entry in game.ledger:
        source = game.objects.get(entry.get("source"))
        if entry.get("kind") == "static" and entry.get("controller") == seat and "unspent" in str(entry.get("text", "")).lower() \
                and source is not None and source.zone == "battlefield":
            return source.name
    return None


def empty_pools(game: Game, *, entering: str | None) -> None:
    """Rule 500.4: unspent mana empties as each step ends. What an effect keeps longer goes when it says:
    as combat ends, or with the turn (`entering` is None when a new turn begins). Mana an effect says is
    never lost ("kept") stays.

    The table reads no card. As with the deaths it is sure of only while nothing on the ledger speaks of
    prevention, it empties a pool only while nothing of that player's speaks of unspent mana: with Ozai, the
    Phoenix King out ("if you would lose unspent mana, that mana becomes red instead") it twice emptied eight
    firebending mana. Such mana is "held": left alone, said so once, and gone with the first step that ends
    after the effect has."""
    for seat in game.seats:
        keeper = _keeper(game, seat.index) if game.mechanics >= 6 else None

        def goes(floating: list, keeper=keeper) -> bool:
            if floating[1] == "kept" and game.mechanics < 7:
                return False
            if floating[1] in ("kept", "held"):
                # No mana is kept for ever: it is always some permanent's doing (Kruphix, Omnath, Leyline Tyrant, Ozai), and it
                # goes with the first step that ends after that permanent has. A pilot who said "kept" with Ozai out kept
                # four red through the phase Ozai died in, and the auditor had it removed a turn later.
                return keeper is None
            return entering is None or floating[1] == "step" or (floating[1] == "end_of_combat" and entering in ("main2", "end", "cleanup"))
        gone = [m for m in seat.pool if goes(m)]
        shown = "".join(f"{{{m[0]}}}" for m in gone)
        if gone and keeper:
            for floating in gone:
                floating[1] = "held"
            game.announcements.append(f"{seat.name}'s unspent {shown} would empty now, but {keeper} speaks of unspent mana, so the table leaves it: "
                                      "its controller removes or changes what that effect does not keep.")
        elif gone:
            seat.pool = [m for m in seat.pool if not goes(m)]
            game.announcements.append(f"{seat.name}'s unspent {shown} empties from their mana pool.")


def spend(game: Game, seat: int, wanted: list[str]) -> None:
    """Take this mana out of the seat's pool, the soonest to empty first."""
    pool = game.seats[seat].pool
    for color in wanted:
        held = sorted((m for m in pool if m[0] == color), key=lambda m: _SOONEST.index(m[1]))
        if not held:
            floating = "".join(f"{{{m[0]}}}" for m in pool) or "nothing"
            raise TableError("not_in_pool", f"You have no {{{color}}} floating: your mana pool holds {floating}.")
        pool.remove(held[0])


def source(game: Game, obj: Obj, lookup: Callable[[str], dict | None] | None) -> dict | None:
    """What tapping this permanent for mana gives: {"colors", "amount"}, or None if it makes none the table knows of."""
    if obj.token is not None:
        text = str(obj.token.get("text", "")).lower()
        if "any color" in text or obj.name == "Treasure":
            return {"colors": set(COLORS), "amount": 1}
        return None
    told = (lookup(obj.name) if lookup and not obj.face_down else None) or {}
    if not told.get("produces"):
        return None
    return {"colors": set(told["produces"]), "amount": int(told.get("mana_amount") or 1)}


def pays(cost: tuple[int, list[set[str]]], sources: list[dict]) -> bool:
    """Can these sources, all of them tapped, produce the cost? Small enough to try every assignment."""
    generic, colored = cost
    pool = [set(s["colors"]) for s in sources for _ in range(s["amount"])]
    if len(pool) < generic + len(colored):
        return False

    def place(index: int, free: list[set[str]]) -> bool:
        if index == len(colored):
            return len(free) >= generic
        return any(place(index + 1, free[:i] + free[i + 1:]) for i, colors in enumerate(free)
                   if colors & colored[index] or ("C" in colored[index] and "C" in colors))
    return place(0, sorted(pool, key=len))


def plan(cost: tuple[int, list[set[str]]], sources: list[dict]) -> list[dict] | None:
    """Which of `sources` to tap for the cost, or None if they cannot pay it. The least flexible
    sources go first, so that a land that makes any color is kept for later."""
    generic, colored = cost
    order = sorted(sources, key=lambda s: (len(s["colors"]), s["amount"]))

    def place(index: int, free: list[dict], used: list[dict], spare: int) -> list[dict] | None:
        if index == len(colored):
            owed = generic - spare
            for source in free:
                if owed <= 0:
                    break
                used, owed = [*used, source], owed - source["amount"]
            return used if owed <= 0 else None
        for i, source in enumerate(free):
            if source["colors"] & colored[index] or ("C" in colored[index] and "C" in source["colors"]):
                found = place(index + 1, free[:i] + free[i + 1:], [*used, source], spare + source["amount"] - 1)
                if found is not None:
                    return found
        return None
    return place(0, order, [], 0)


def payment_problem(game: Game, seat: int, told: dict | None, tapped: list[int], *, x: int = 0, tax: int = 0,
                    lookup: Callable[[str], dict | None] | None = None, floating: list[str] | None = None) -> str | None:
    """Why the permanents tapped cannot pay this card's cost, as far as the table can tell; None if
    they can, or if the table does not know enough to say."""
    cost = parse_cost((told or {}).get("mana_cost") or "", x) if told else None
    if cost is None or not told.get("mana_cost"):
        return None
    sources = [source(game, game.objects[i], lookup) for i in tapped if i in game.objects]
    if any(s is None for s in sources):
        return None  # something tapped makes mana in a way the table was not told about
    sources += [{"colors": {color}, "amount": 1} for color in floating or []]
    cost = (cost[0] + tax, cost[1])
    if pays(cost, sources):
        return None
    made = ", ".join("/".join(sorted(s["colors"], key=(COLORS + "C").index)) + (f" x{s['amount']}" if s["amount"] > 1 else "") for s in sources)
    return (f"{told['mana_cost']}" + (f" plus {{{tax}}} commander tax" if tax else "") + f" cannot be paid with what you tapped ({made or 'nothing'}).")


def affordable(game: Game, seat: int, cards: list[tuple[str, dict | None, int]], lookup) -> list[tuple[str, list[int]]]:
    """Of `cards` (name, what the table was told, commander tax), those the seat's untapped mana
    sources could pay for right now, before any cost changes, each with the permanents to tap for it."""
    ready = []
    for obj in (game.objects[i] for i in game.battlefield):
        if obj.controller != seat or obj.tapped:
            continue
        made = source(game, obj, lookup)
        told = (lookup(obj.name) if lookup and obj.token is None else None) or {}
        sick = "Creature" in told.get("types", []) and obj.entered_turn == game.turn.number and "Haste" not in told.get("keywords", [])
        if made and not sick and obj.token is None:  # a Treasure is not tapped for mana but sacrificed: left to the player
            ready.append({**made, "id": obj.id})
    ready += [{"colors": {m[0]}, "amount": 1, "id": None, "floating": m[0]} for m in game.seats[seat].pool]
    found = []
    for name, told, tax in cards:
        cost = parse_cost((told or {}).get("mana_cost") or "") if told and "Land" not in told.get("types", []) else None
        tapped = plan((cost[0] + tax, cost[1]), ready) if cost is not None else None
        if tapped is not None:
            found.append((name, [s["id"] for s in tapped if s["id"] is not None], "".join(s["floating"] for s in tapped if s.get("floating"))))
    return found
