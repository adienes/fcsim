"""Event triggers as patterns over what the table sees happen.

Magic has thousands of trigger conditions, and the table must not grow a case for each. It does
not need to: the table can only ever see a handful of things happen, because it only has a handful
of mutations. An object moves between zones; damage is dealt; life changes; counters change; a
permanent taps or untaps; a creature attacks or blocks; a spell is cast. Every mutation records one
such fact. A hook's `on` is a pattern over those facts, all of it data:

    {"event": "moved", "from": "library", "to": "graveyard", "not_type": "Land", "once": true}

"enters", "dies", "milled", "discarded" and the rest are names for common patterns (ALIASES), not
code. A condition no pattern can state ("your second spell each turn") stays what it always was:
text on the ledger, which the auditor checks after the fact. A loose match is safe, since the
controller declines a trigger that does not apply; a missing one is what the auditor is for.
"""

from __future__ import annotations

from .model import Game

ALIASES: dict[str, dict] = {
    "cast": {"event": "cast"},
    "attacks": {"event": "attacks"},
    "blocks": {"event": "blocks"},
    "enters": {"event": "moved", "to": "battlefield", "not_from": "battlefield"},
    "leaves": {"event": "moved", "from": "battlefield", "not_to": "battlefield"},
    "dies": {"event": "moved", "from": "battlefield", "to": "graveyard", "type": "Creature"},
    "milled": {"event": "moved", "from": "library", "to": "graveyard"},
    "discarded": {"event": "moved", "from": "hand", "to": "graveyard"},
    "drawn": {"event": "moved", "from": "library", "to": "hand"},
    "exiled": {"event": "moved", "to": "exile", "not_from": "exile"},
    "returned_to_hand": {"event": "moved", "from": "battlefield", "to": "hand"},
    "deals_combat_damage": {"event": "damage", "combat": True, "to": "player"},
    "deals_damage": {"event": "damage"},
    "gains_life": {"event": "life", "sign": "gain"},
    "loses_life": {"event": "life", "sign": "loss"},
    "counters_added": {"event": "counters", "sign": "gain"},
    "counters_removed": {"event": "counters", "sign": "loss"},
    "tapped": {"event": "tapped"},
    "untapped": {"event": "untapped"},
}
_PUBLIC_ZONES = {"battlefield", "graveyard", "exile", "stack", "command"}


def _as_list(value) -> list[str]:
    return [] if value in (None, "", False) else [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]


def pattern(on: dict) -> dict:
    """A hook's `on` with its alias written out. What the hook adds narrows the alias; types add up."""
    named = ALIASES.get(str(on.get("event", "")), {"event": on.get("event")})
    merged = {**named, **{k: v for k, v in on.items() if k != "event"}}
    merged["type"] = _as_list(named.get("type")) + [t for t in _as_list(on.get("type")) if t not in _as_list(named.get("type"))]
    merged["not_type"] = _as_list(on.get("not_type")) + (["Land"] if on.get("nonland") else [])
    return merged


def matches(on: dict, fact: dict, hook: dict) -> bool:
    """Whether the fact fits the hook's pattern, or any of the alternatives it lists under `any_of`
    ("whenever a player casts a spell or a creature attacks" is one ability, so one hook)."""
    alternatives = [a for a in on.get("any_of") or [] if isinstance(a, dict)]
    if alternatives:
        shared = {k: v for k, v in on.items() if k != "any_of"}
        return (bool(shared.get("event")) and _fits(shared, fact, hook)) or any(_fits({**{k: v for k, v in shared.items() if k != "event"}, **a}, fact, hook)
                                                                             for a in alternatives)
    return _fits(on, fact, hook)


def _fits(on: dict, fact: dict, hook: dict) -> bool:
    want = pattern(on)
    if not want.get("event") or want["event"] != fact.get("event"):
        return False
    for key in ("from", "to", "kind"):
        if want.get(key) is not None and fact.get(key) is not None and str(want[key]).lower() != str(fact[key]).lower():
            return False
        if want.get(f"not_{key}") is not None and str(want[f"not_{key}"]).lower() == str(fact.get(key)).lower():
            return False
    type_line = str(fact.get("type_line") or "").lower()
    if type_line and any(t.lower() not in type_line for t in want["type"]):  # a type the table was not told is given the benefit of the doubt
        return False
    if any(t.lower() in type_line for t in want["not_type"]):
        return False
    actor = fact.get("was_controller") if fact["event"] == "moved" and fact.get("to") != "battlefield" else fact.get("controller")
    for key, other in (("who", actor), ("whom", fact.get("to_player"))):
        if want.get(key) in ("you", "opponent") and other is not None and (want[key] == "you") != (other == hook.get("controller")):
            return False
    ids = (fact.get("id"), fact.get("old_id"))
    if (want.get("other") and hook.get("source") in ids) or (want.get("self") and hook.get("source") not in ids):
        return False
    if want.get("nontoken") and fact.get("token"):
        return False
    if "combat" in want and bool(want["combat"]) != bool(fact.get("combat")):
        return False
    if want.get("sign") and (want["sign"] == "gain") != (int(fact.get("delta") or 0) > 0):
        return False
    return not want.get("min") or abs(int(fact.get("amount") or fact.get("delta") or 0)) >= int(want["min"])


def because(game: Game, fact: dict) -> str:
    """What happened, in words, for the trigger's controller. A card that moved between hidden zones is not named."""
    name = fact.get("name") or "something"
    if fact["event"] == "moved":
        origin, to = fact.get("from"), fact.get("to")
        if origin not in _PUBLIC_ZONES and to not in _PUBLIC_ZONES:
            name = "a card"
        told = {("library", "graveyard"): "was milled", ("hand", "graveyard"): "was discarded", ("library", "hand"): "was drawn"}.get((origin, to))
        if told is None:
            told = "entered" if to == "battlefield" else "died" if (origin, to) == ("battlefield", "graveyard") else \
                "was exiled" if to == "exile" else "left the battlefield" if origin == "battlefield" else f"went from {origin} to {to}"
        return f"{name} {told}"
    if fact["event"] == "damage":
        target = game.seats[fact["to_player"]].name if fact.get("to_player") is not None else "a permanent"
        return f"{name} dealt {fact.get('amount')} {'combat ' if fact.get('combat') else ''}damage to {target}"
    if fact["event"] == "life":
        return f"{name} {'gained' if int(fact.get('delta') or 0) > 0 else 'lost'} {abs(int(fact.get('delta') or 0))} life"
    if fact["event"] == "counters":
        return f"{name} {'got' if int(fact.get('delta') or 0) > 0 else 'lost'} {abs(int(fact.get('delta') or 0))} {fact.get('kind')} counter(s)"
    return f"{name} " + {"cast": "was cast", "attacks": "attacked", "blocks": "blocked", "tapped": "became tapped", "untapped": "untapped"}.get(fact["event"], fact["event"])


def fact(event: str, *, name: str, controller: int | None, obj=None, **more) -> dict:
    """One thing the table saw happen. Every fact has the same core, so a pattern never meets a missing key."""
    return {"event": event, "id": obj.id if obj is not None else None, "old_id": None, "name": name,
            "type_line": obj.type_line if obj is not None else "", "controller": controller, "was_controller": controller,
            "token": bool(obj is not None and obj.token is not None), **more}
