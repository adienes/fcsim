"""The primitive vocabulary agents use to change the table, and the zone rules behind it.

Every function takes ids the caller has already been shown to be allowed to name. Whether an
effect really says to do this is for the agents, the auditor and the judge; what is enforced
here is that cards are conserved and hidden information only flows where a call says it does.
"""

from __future__ import annotations

from .model import HIDDEN_ZONES, PUBLIC_ZONES, SEAT_ZONES, STEPS, Dice, Game, Obj, TableError

ZONES = (*SEAT_ZONES, "battlefield", "stack")
_COMBAT_PHASE = ["begin_combat", "declare_attackers", "declare_blockers", "combat_damage", "end_combat"]


def _zone_list(game: Game, obj: Obj) -> list[int]:
    return game.battlefield if obj.zone == "battlefield" else \
        [] if obj.zone == "stack" else getattr(game.seats[obj.owner], obj.zone)


def get(game: Game, obj_id: int) -> Obj:
    try:
        return game.objects[obj_id]
    except KeyError:
        raise TableError("gone", "That object is no longer there; it changed zones. Look at the table again.") from None


def renew(game: Game, obj: Obj) -> Obj:
    """Give the object a new identity, in place in its zone. Old handles and references die."""
    old = obj.id
    del game.objects[old]
    obj.id = game.new_id()
    game.objects[obj.id] = obj
    zone = _zone_list(game, obj)
    if old in zone:
        zone[zone.index(old)] = obj.id
    for item in game.stack:
        if item.obj == old:
            item.obj = obj.id
    return obj


def move(game: Game, obj: Obj, to: str, *, position: str | int = "top", controller: int | None = None,
         enters: dict | None = None, face_down: bool = False, known_by: list[int] | None = None) -> Obj | None:
    """Move an object to a zone. Cards go to their owner's zones, and a token that leaves the
    battlefield ceases to exist. Returns the new object, or None for a token that vanished."""
    if to not in ZONES:
        raise TableError("bad_zone", f"Unknown zone {to!r}. Zones are: {', '.join(ZONES)}.")
    source = _zone_list(game, obj)
    if obj.id in source:
        source.remove(obj.id)
    knew = set(range(len(game.seats))) if obj.public else set(obj.known_by)
    came_from = obj.zone
    for other in game.objects.values():
        if other.attached_to == obj.id:
            other.attached_to = None
    game.stack[:] = [item for item in game.stack if item.obj != obj.id or to == "stack"]
    if obj.token is not None and to != "battlefield" and came_from == "battlefield":
        del game.objects[obj.id]
        return None

    obj.zone = to
    obj.tapped = obj.transformed = obj.phased_out = False
    obj.counters, obj.annotations, obj.damage, obj.attached_to = {}, [], 0, None
    obj.controller = obj.owner if to != "battlefield" and to != "stack" else \
        (controller if controller is not None else obj.controller)
    obj.entered_turn = game.turn.number if to == "battlefield" else None
    obj.face_down = face_down and to in ("battlefield", "exile", "stack")
    if to in PUBLIC_ZONES:
        obj.public = not obj.face_down
        obj.known_by = set(known_by if known_by is not None else [obj.controller]) if obj.face_down else set()
    elif to == "hand":
        obj.public, obj.known_by = False, {obj.owner}
    else:  # library: whoever knew the card still knows where it is, until a shuffle
        placed = position in ("top", "bottom") or isinstance(position, int)
        obj.public, obj.known_by = False, (set(known_by) if known_by is not None else knew) if placed else set()
    if to == "battlefield":
        enters = enters or {}
        obj.tapped = bool(enters.get("tapped"))
        obj.counters = {kind: int(n) for kind, n in (enters.get("counters") or {}).items()}
        obj.attached_to = enters.get("attached_to")
        game.battlefield.append(obj.id)
    elif to != "stack":
        target = getattr(game.seats[obj.owner], to)
        if to in ("library", "graveyard"):
            # Any other position means "about to be shuffled": where it sits for now is irrelevant.
            index = 0 if position == "top" else int(position) if isinstance(position, int) else len(target)
            target.insert(max(0, min(index, len(target))), obj.id)
        else:
            target.append(obj.id)
    return renew(game, obj)


def shuffle(game: Game, seat: int, dice: Dice, *, only_unknown: bool = False) -> None:
    """Shuffle a library. With `only_unknown`, cards whose position someone knows stay where
    they are and the rest are shuffled among the other positions (used after a rewind)."""
    library = game.seats[seat].library
    movable = [i for i, obj_id in enumerate(library)
               if not only_unknown or not (game.objects[obj_id].known_by or game.objects[obj_id].public)]
    order = dice.order(len(movable))
    cards = [library[movable[i]] for i in order]
    for slot, obj_id in zip(movable, cards, strict=True):
        library[slot] = obj_id
    for slot in movable:
        obj = game.objects[library[slot]]
        obj.public, obj.known_by = False, set()
        renew(game, obj)


def draw(game: Game, seat: int, count: int = 1) -> list[Obj]:
    drawn = []
    for _ in range(count):
        library = game.seats[seat].library
        if not library:
            game.seats[seat].drew_from_empty = True
            break
        drawn.append(move(game, game.objects[library[0]], "hand"))
    return drawn


def from_position(game: Game, where: dict, dice: Dice) -> list[Obj]:
    """Objects named by place rather than by handle: the top or bottom of a library, or cards
    at random from a hand. This is how an effect reaches a zone the caller cannot see into."""
    seat, zone, count = where["seat"], where.get("zone", "library"), int(where.get("count", 1))
    if zone not in HIDDEN_ZONES or not 0 <= seat < len(game.seats) or count < 1:
        raise TableError("bad_position", "Use {seat, zone: 'library', position: 'top'|'bottom', count} "
                                         "or {seat, zone: 'hand', position: 'random', count}.")
    cards = getattr(game.seats[seat], zone)
    position = where.get("position", "top" if zone == "library" else "random")
    if zone == "library" and position in ("top", "bottom"):
        chosen = cards[:count] if position == "top" else cards[-count:]
    elif zone == "hand" and position == "random":
        pool = sorted(cards)  # a fixed order, so the recorded picks mean the same cards on replay
        chosen = [pool.pop(dice.pick(len(pool))) for _ in range(min(count, len(pool)))]
    else:
        raise TableError("bad_position", f"A {zone} cannot be addressed by position {position!r}.")
    return [game.objects[obj_id] for obj_id in chosen]


def create_token(game: Game, controller: int, definition: dict, *, enters: dict | None = None) -> Obj:
    obj = Obj(id=game.new_id(), uid=game.new_id(), owner=controller, controller=controller, zone="battlefield",
              name=definition.get("name", "Token"), token=dict(definition), public=True,
              entered_turn=game.turn.number, tapped=bool((enters or {}).get("tapped")),
              counters={k: int(n) for k, n in ((enters or {}).get("counters") or {}).items()},
              attached_to=(enters or {}).get("attached_to"))
    game.objects[obj.id] = obj
    game.battlefield.append(obj.id)
    return obj


def eliminate(game: Game, seat: int, status: str = "eliminated") -> None:
    """A player leaves: everything they own leaves with them, and their spells leave the stack."""
    player = game.seats[seat]
    if player.status != "alive":
        return
    player.status = status
    for obj in [o for o in game.objects.values() if o.owner == seat]:
        zone = _zone_list(game, obj)
        if obj.id in zone:
            zone.remove(obj.id)
        del game.objects[obj.id]
    for obj in game.objects.values():
        if obj.controller == seat:
            obj.controller = obj.owner
        if obj.attached_to is not None and obj.attached_to not in game.objects:
            obj.attached_to = None
    game.stack[:] = [item for item in game.stack if item.controller != seat]
    game.pending_triggers[:] = [t for t in game.pending_triggers if t["seat"] != seat]
    game.turn.extra_turns[:] = [s for s in game.turn.extra_turns if s != seat]
    for key, holder in list(game.designations.items()):
        if holder == seat:
            game.designations[key] = None
    living = game.alive()
    if len(living) <= 1:
        game.winner = living[0] if living else None
        game.control.kind, game.control.seats, game.control.detail = "over", [], {}


def insert_combat(game: Game) -> None:
    """An additional combat phase after the current phase, followed by an additional main phase."""
    game.turn.remaining[:0] = [*_COMBAT_PHASE, "main2"]


def end_turn(game: Game) -> None:
    game.turn.remaining[:] = ["cleanup"]


def skip_step(game: Game, step: str) -> None:
    if step not in STEPS:
        raise TableError("bad_step", f"Unknown step {step!r}. Steps are: {', '.join(STEPS)}.")
    if step in game.turn.remaining:
        game.turn.remaining.remove(step)
