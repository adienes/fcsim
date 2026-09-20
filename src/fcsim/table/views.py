"""What one viewer may see of the table, and the names they use to point at it.

Internal ids never leave the server. Each viewer has their own handles, issued only for things
they can tell apart, so neither an id nor its absence says anything about hidden cards.
"""

from __future__ import annotations

from .model import Game, Obj, StackItem


class Handles:
    """One viewer's handles. `c` objects, `s` stack items, `q` questions, `t` waiting triggers."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, int], str] = {}
        self._by_handle: dict[str, tuple[str, int]] = {}

    def of(self, kind: str, internal: int) -> str:
        key = (kind, internal)
        if key not in self._by_key:
            handle = f"{kind}{len(self._by_key) + 1}"
            self._by_key[key], self._by_handle[handle] = handle, key
        return self._by_key[key]

    def resolve(self, kind: str, handle) -> int | None:
        found = self._by_handle.get(handle) if isinstance(handle, str) else None
        return found[1] if found and found[0] == kind else None


def obj_view(game: Game, obj: Obj, viewer: int | None, handles: Handles) -> dict:
    view: dict = {"handle": handles.of("c", obj.id), "owner": obj.owner}
    if obj.face_known_to(viewer):
        view["name"] = obj.name
        if obj.type_line:
            view["type_line"] = obj.type_line
        if obj.token is not None:
            view["token"] = obj.token
        if obj.is_commander:
            view["is_commander"] = True
    if obj.face_down:
        view["face_down"] = True
    if obj.zone == "battlefield":
        view["controller"] = obj.controller
        view["tapped"] = obj.tapped
        view["entered_this_turn"] = obj.entered_turn == game.turn.number
        attached = game.objects.get(obj.attached_to) if obj.attached_to is not None else None
        for key, value in (("counters", obj.counters), ("damage", obj.damage), ("annotations", obj.annotations),
                           ("computed", obj.computed),
                           ("transformed", obj.transformed), ("phased_out", obj.phased_out),
                           ("attached_to", handles.of("c", attached.id) if attached else None)):
            if value:
                view[key] = value
    return view


def _target_view(game: Game, target: dict, handles: Handles) -> dict:
    if "seat" in target:
        return {"seat": target["seat"]}
    if "item" in target:
        live = any(item.id == target["item"] for item in game.stack)
        return {"stack_item": handles.of("s", target["item"])} if live else {"gone": True}
    obj = game.objects.get(target.get("obj", -1))
    return {"object": handles.of("c", obj.id), "name": obj.name if obj.public else None} if obj else {"gone": True}


def item_view(game: Game, item: StackItem, handles: Handles) -> dict:
    view = {"handle": handles.of("s", item.id), "kind": item.kind, "controller": item.controller,
            "source": item.source_name, "text": item.text,
            "targets": [_target_view(game, t, handles) for t in item.targets]}
    if item.obj in game.objects:  # the card itself, which is what a counterspell moves
        view["card"] = handles.of("c", item.obj)
    for key in ("x", "modes", "payment"):
        if getattr(item, key):
            view[key] = getattr(item, key)
    return view


def _ledger_view(game: Game, handles: Handles) -> list[dict]:
    entries = []
    for entry in game.ledger:
        shown = {k: v for k, v in entry.items() if k != "source"}
        if entry.get("source") in game.objects:
            shown["source"] = handles.of("c", entry["source"])
        entries.append(shown)
    return entries


def _zone(game: Game, ids: list[int], viewer: int | None, handles: Handles) -> list[dict]:
    return [obj_view(game, game.objects[i], viewer, handles) for i in ids]


def _hidden_zone(game: Game, seat: int, zone: str, viewer: int | None, handles: Handles) -> dict:
    ids = getattr(game.seats[seat], zone)
    view: dict = {"count": len(ids)}
    known = [(index, game.objects[i]) for index, i in enumerate(ids) if game.objects[i].addressable_by(viewer)]
    if zone == "hand" and viewer == seat:
        view["cards"] = [obj_view(game, obj, viewer, handles) for _, obj in known]
    elif zone == "hand" and known:  # revealed cards, in an order that says nothing about the hand
        view["known"] = [obj_view(game, obj, viewer, handles)
                         for _, obj in sorted(known, key=lambda pair: (pair[1].name, pair[1].id))]
    elif known:  # a library card is known by where it sits
        view["known"] = [{"from_top": index, **obj_view(game, obj, viewer, handles)} for index, obj in known]
    return view


def view(game: Game, viewer: int | None, handles: Handles, rev: int) -> dict:
    """Everything `viewer` may know. `None` is the public view the auditor and judge get."""
    turn, control = game.turn, game.control
    seats = []
    for seat in game.seats:
        seats.append({
            "seat": seat.index, "name": seat.name, "status": seat.status, "life": seat.life,
            "counters": seat.counters, "commander_damage": seat.commander_damage,
            "commander_casts": seat.commander_casts, "mulligans": seat.mulligans,
            **({"response_policy": seat.response_policy, "holds_for_end_step": seat.holds_for_end_step}
               if viewer == seat.index else {}),  # whether one holds up interaction is private
            "hand": _hidden_zone(game, seat.index, "hand", viewer, handles),
            "library": _hidden_zone(game, seat.index, "library", viewer, handles),
            "graveyard": _zone(game, seat.graveyard, viewer, handles),
            "exile": _zone(game, seat.exile, viewer, handles),
            "command": _zone(game, seat.command, viewer, handles),
        })
    result = {
        "rev": rev, "you": viewer,
        "turn": {"number": turn.number, "round": game.seats[turn.active].turns_taken, "active": turn.active,
                 "step": turn.step, "land_plays": turn.land_plays,
                 "passed": turn.passed, "extra_turns": turn.extra_turns,
                 "attackers": [{"attacker": handles.of("c", a["attacker"]), "target": _target_view(game, a["target"], handles)}
                               for a in turn.attackers if a["attacker"] in game.objects],
                 "blockers": [{"blocker": handles.of("c", b["blocker"]), "attacker": handles.of("c", b["attacker"])}
                              for b in turn.blockers if b["blocker"] in game.objects and b["attacker"] in game.objects]},
        "pen": {"kind": control.kind, "seats": control.seats,
                **{k: v for k, v in control.detail.items() if k in ("action", "step", "because", "finding", "final", "objection")}},
        "seats": seats,
        "battlefield": _zone(game, game.battlefield, viewer, handles),
        "stack": [item_view(game, item, handles) for item in game.stack],
        "resolving": item_view(game, game.resolution["item"], handles) if game.resolution else None,
        "questions": [{"handle": handles.of("q", r["id"]), "from": r["from"], "prompt": r["prompt"],
                       "options": r["options"], "answers": r["answers"], "waiting": r["waiting"]}
                      for r in game.requests],
        "waiting_triggers": [{"handle": handles.of("t", t["id"]), "seat": t["seat"], "text": t["text"]}
                             for t in game.pending_triggers],
        "ledger": _ledger_view(game, handles), "rulings": game.rulings, "designations": game.designations,
        "winner": game.winner,
    }
    return result
