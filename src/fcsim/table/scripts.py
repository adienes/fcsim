"""Scripted effects: a triggered ability whose whole effect the table can carry out itself.

Most triggers in a game have no decision in them: "each player gets a rad counter", "put a +1/+1
counter on each creature you control", Norin the Wary's exile and return. Each still cost a wake
to put on the stack and another to resolve. A hook may instead carry its effect as a short script
over a few operations the table already models, with symbolic subjects in place of handles. The
table then puts the trigger on the stack and resolves it with no model in the loop.

Only what is certain is scripted. The vocabulary below is all a script may use; anything outside
it is refused here, and whether a card's ability may be scripted at all is decided elsewhere, from
its printed text and the auditor's repeated agreement (see `fcsim.play.pod`). A script that finds
its subject gone does nothing, which is what the rules say of most such effects.
"""

from __future__ import annotations

from .model import Game, TableError

OBJECTS = ("self", "it", "each_creature_you_control", "each_other_creature_you_control")
PLAYERS = ("you", "each_opponent", "each_player", "its_controller")
STEPS = ("upkeep", "draw", "main1", "begin_combat", "end_combat", "main2", "end")
ZONES = ("battlefield", "graveyard", "exile", "hand", "command")
_SHAPES = {  # operation -> the fields it must have, each with what it may hold
    "counters": {"on": OBJECTS + PLAYERS, "kind": str, "delta": int},
    "life": {"who": PLAYERS, "delta": int},
    "draw": {"who": PLAYERS, "count": int},
    "mill": {"who": PLAYERS, "count": int},
    "create_token": {"token": dict, "count": int},
    "move": {"what": ("self", "it"), "to": ZONES},
    "tap": {"what": ("self", "it")}, "untap": {"what": ("self", "it")},
    "delay": {"step": STEPS, "text": str, "script": list},
}


def problem(script) -> str | None:
    """Why this is not a script the table will run, or None if it is one."""
    if not isinstance(script, list) or not 1 <= len(script) <= 6:
        return "a script is a list of one to six operations"
    for step in script:
        if not isinstance(step, dict) or step.get("op") not in _SHAPES:
            return f"{step!r} is not one of: {', '.join(_SHAPES)}"
        shape = _SHAPES[step["op"]]
        if set(step) - {"op", *shape, "whose"}:
            return f"`{step['op']}` takes only {', '.join(shape)}"
        for field, allowed in shape.items():
            value = step.get(field, 1 if field == "count" else None)
            if isinstance(allowed, tuple):
                if value not in allowed:
                    return f"`{step['op']}.{field}` is one of: {', '.join(allowed)}"
            elif not isinstance(value, allowed) or isinstance(value, bool):
                return f"`{step['op']}.{field}` must be {allowed.__name__}"
        if step["op"] == "delay" and (nested := problem(step["script"])):
            return f"in the delayed script: {nested}"
        if step["op"] == "delay" and any(s["op"] == "delay" for s in step["script"]):
            return "a delayed script cannot delay again"
    return None


def _objects(game: Game, who: str, controller: int, bound: dict) -> list:
    if who in ("self", "it"):
        uid = bound.get(who)
        return [o for o in game.objects.values() if o.uid == uid and uid is not None]
    mine = [game.objects[i] for i in game.battlefield if game.objects[i].controller == controller
            and "creature" in game.objects[i].type_line.lower()]
    return [o for o in mine if who == "each_creature_you_control" or o.uid != bound.get("self")]


def _players(game: Game, who: str, controller: int, bound: dict) -> list[int]:
    alive = [s.index for s in game.seats if s.status == "alive"]
    if who == "its_controller":
        found = _objects(game, "it", controller, bound)
        return [found[0].controller] if found else []
    return [s for s in alive if who == "each_player" or (who == "you") == (s == controller)]


def run(game: Game, script: list[dict], controller: int, bound: dict, apply) -> None:
    """Carry the script out as `controller`. `bound` maps "self" and "it" to the uid a card keeps
    across zones; `apply(op)` applies one concrete table mutation. A subject that is no longer where
    the effect expects it is left alone."""
    for step in script:
        op = step["op"]
        if op == "counters":
            for obj in (_objects(game, step["on"], controller, bound) if step["on"] in OBJECTS else []):
                if obj.zone == "battlefield":
                    apply({"op": "counters", "obj": obj.id, "kind": step["kind"], "delta": step["delta"]})
            for seat in (_players(game, step["on"], controller, bound) if step["on"] in PLAYERS else []):
                apply({"op": "counters", "seat": seat, "kind": step["kind"], "delta": step["delta"]})
        elif op in ("life", "draw", "mill"):
            for seat in _players(game, step["who"], controller, bound):
                apply({"op": op, "seat": seat, **({"delta": step["delta"]} if op == "life" else {"count": step.get("count", 1)})})
        elif op == "create_token":
            apply({"op": "create_token", "token": step["token"], "count": step.get("count", 1), "controller": controller})
        elif op in ("tap", "untap"):
            for obj in _objects(game, step["what"], controller, bound):
                if obj.zone == "battlefield":
                    apply({"op": op, "objs": [obj.id]})
        elif op == "move":
            for obj in _objects(game, step["what"], controller, bound):
                expected = "battlefield" if step["to"] != "battlefield" else "exile"
                if obj.zone == expected:  # "exile it" finds it on the battlefield; "return it" finds it where it was sent
                    apply({"op": "move", "obj": obj.id, "to": step["to"], **({"controller": obj.owner} if step["to"] == "battlefield" else {})})
        elif op == "delay":
            whose = controller if step.get("whose") == "mine" else "any"
            game.ledger.append({"kind": "delayed", "step": step["step"], "whose": whose, "controller": controller, "text": step["text"],
                                "source": None, "script": step["script"], "bound": dict(bound)})
        else:  # pragma: no cover - `problem` refuses anything else before a script is ever stored
            raise TableError("bad_script", f"Unknown scripted operation {op!r}.")
