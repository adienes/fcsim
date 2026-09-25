"""Apply one mutation from a batch, and say what happened: publicly, and privately to those who may know."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import mana, mutations, triggers, turn
from .model import HIDDEN_ZONES, Dice, Game, Obj, TableError


@dataclass
class Told:
    """What a batch produced: a public sentence per op, private sentences by seat, objects the
    caller should be shown, and plain values (rolls, flips) to return."""
    public: list[str] = field(default_factory=list)
    private: dict[int, list[str]] = field(default_factory=dict)
    show: list[int] = field(default_factory=list)
    values: list = field(default_factory=list)

    def secretly(self, seats, sentence: str) -> None:
        for seat in set(seats):
            self.private.setdefault(seat, []).append(sentence)


def _seat(game: Game, value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < len(game.seats):
        raise TableError("bad_seat", f"Seats are numbered 0 to {len(game.seats) - 1}.")
    return value


def _count(op: dict, key: str = "count", default: int = 1) -> int:
    value = op.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 200:
        raise TableError("bad_count", f"{key} must be a whole number from 0 to 200.")
    return value


def _who(game: Game, seat: int) -> str:
    return game.seats[seat].name


def _named(obj: Obj) -> str:
    return obj.name if obj.public else "a face-down card" if obj.face_down else "a card"


def _where(game: Game, obj: Obj) -> str:
    return "the battlefield" if obj.zone == "battlefield" else "the stack" if obj.zone == "stack" \
        else f"{_who(game, obj.owner)}'s {obj.zone}"


def _selected(game: Game, dice: Dice, actor: int, op: dict) -> list[Obj]:
    """The objects an op names: by handle (`obj`, `objs`) or by place (`from`)."""
    if "from" in op:
        where = op["from"]
        if where.get("seat") != actor and not op.get("because"):
            raise TableError("because_required", "Reaching into another player's hidden zone needs a "
                                                 "`because` naming the effect that allows it.")
        return mutations.from_position(game, where, dice)
    ids = op.get("objs") if "objs" in op else [op["obj"]] if "obj" in op else None
    if not ids:
        raise TableError("no_object", f"`{op.get('op')}` needs `obj`, `objs`, or `from` (a position in a hidden zone).")
    return [mutations.get(game, i) for i in ids]


def apply_op(game: Game, dice: Dice, actor: int, op: dict, told: Told, *, by_table: bool = False) -> None:
    kind, me = op.get("op"), _who(game, actor)
    because = f" ({op['because']})" if op.get("because") else ""
    match kind:
        case "move":
            for obj in _selected(game, dice, actor, op):
                if op.get("to") == "stack" and obj.zone != "stack" and game.mechanics >= 6 and not by_table:
                    raise TableError("cast_it", f"{_named(obj)} gets onto the stack by being cast, not moved: call `cast`, which works during a "
                                                "resolution too (name the effect in `because`; say in the payment's `note` if its mana cost is not "
                                                "paid). A card moved there has no spell to go with it and is in no zone anyone can see.")
                before, origin, knew = _named(obj), _where(game, obj), {obj.owner} | obj.known_by
                hidden_trip = obj.zone in HIDDEN_ZONES and op.get("to") in HIDDEN_ZONES and not op.get("reveal")
                if op.get("reveal"):
                    obj.public = True
                    before = obj.name
                name, to = obj.name, op.get("to", "")
                kept_out = bool(op.get("stay")) and actor == obj.owner  # to reanimate it, or to recast it from exile for less
                replaced = obj.is_commander and to in ("hand", "library") and game.mechanics >= 8 and not kept_out
                to = "command" if replaced else to
                moved = mutations.move(game, obj, to, position=op.get("position", "top"),
                                       controller=op.get("controller"), enters=op.get("enters"),
                                       face_down=bool(op.get("face_down")),
                                       known_by=[actor] if hidden_trip and obj.zone == "library" else op.get("known_by"))
                if moved is not None and moved.is_commander and kept_out:
                    moved.stays = True
                seen = moved is not None and moved.public
                told.public.append(f"{me} moves {name if seen else before} from {origin} to "
                                   f"{_where(game, moved) if moved else 'nowhere (a token ceases to exist)'}{because}.")
                if replaced:
                    told.public.append(f"{name} is a commander: its owner puts it into the command zone instead of {op.get('to')} (rule 903.9b).")
                if moved is not None and not seen and before != name:
                    told.secretly((knew | moved.known_by) - {None}, f"That card is {name}.")
                if moved is not None and moved.face_known_to(actor):
                    told.show.append(moved.id)
        case "tap" | "untap":
            objs = _selected(game, dice, actor, op)
            for obj in objs:
                if obj.tapped != (kind == "tap"):
                    game.facts.append(triggers.fact("tapped" if kind == "tap" else "untapped", name=obj.name, controller=obj.controller, obj=obj))
                obj.tapped = kind == "tap"
            told.public.append(f"{me} {kind}s {', '.join(_named(o) for o in objs)}.")
        case "counters":
            holder = game.seats[_seat(game, op["seat"])] if "seat" in op else _selected(game, dice, actor, op)[0]
            name = str(op.get("kind", ""))
            if not name:
                raise TableError("bad_counter", "Name the kind of counter, e.g. '+1/+1', 'loyalty', 'poison'.")
            value = int(op["set"]) if "set" in op else holder.counters.get(name, 0) + int(op.get("delta", 0))
            on_object = isinstance(holder, Obj)
            game.facts.append(triggers.fact("counters", name=_named(holder) if on_object else holder.name, obj=holder if on_object else None,
                                            controller=holder.controller if on_object else holder.index, kind=name,
                                            delta=value - holder.counters.get(name, 0)))
            holder.counters[name] = value
            if value <= 0:
                del holder.counters[name]
            label = _named(holder) if isinstance(holder, Obj) else holder.name
            told.public.append(f"{label} now has {max(value, 0)} {name} counter(s){because}.")
        case "life":
            player = game.seats[_seat(game, op.get("seat"))]
            was = player.life
            player.life = int(op["set"]) if "set" in op else player.life + int(op.get("delta", 0))
            if player.life != was:
                game.facts.append(triggers.fact("life", name=player.name, controller=player.index, delta=player.life - was))
            told.public.append(f"{player.name} is at {player.life} life{because}.")
        case "create_token":
            controller = _seat(game, op.get("controller", actor))
            definition = dict(op.get("token") or {})
            if "copy_of" in op:
                definition = {"name": mutations.get(game, op["copy_of"]).name, "copy": True, **definition}
            if not definition.get("name"):
                raise TableError("bad_token", "Give the token a definition: {name, types, power, toughness, text}.")
            made = [mutations.create_token(game, controller, definition, enters=op.get("enters"))
                    for _ in range(_count(op))]
            told.show += [obj.id for obj in made]
            told.public.append(f"{me} creates {len(made)} {definition['name']} token(s) for {_who(game, controller)}.")
        case "attach":
            obj = _selected(game, dice, actor, op)[0]
            host = mutations.get(game, op["host"]) if op.get("host") is not None else None
            obj.attached_to = host.id if host else None
            told.public.append(f"{me} attaches {_named(obj)} to {_named(host)}." if host
                               else f"{me} unattaches {_named(obj)}.")
        case "mana":
            # Floating mana had nowhere to live: it was a note on a permanent, and the auditor twice caught it spent twice.
            player = game.seats[_seat(game, op.get("seat", actor))]
            added, removed = mana.parse_mana(op.get("add", "")) if op.get("add") else [], mana.parse_mana(op.get("remove", "")) if op.get("remove") else []
            until, keep = str(op.get("until") or "step"), op.get("keep")
            if added is None or removed is None or not (added or removed or keep) or until not in mana.LIFETIMES or (keep and keep not in mana.LIFETIMES):
                raise TableError("bad_mana", "Give `add` (or `remove`) as mana, such as \"RRG\" or \"{R}{R}{G}\", and `until` as one of: "
                                             + ", ".join(mana.LIFETIMES) + " (the default, step, empties as this step ends); or `keep` alone, "
                                             "with one of those, for what is already in the pool.")
            mana.spend(game, player.index, removed)
            if keep:
                # A spell that says "until end of turn, you don't lose unspent red mana" (The Last Agni Kai) is no permanent the
                # table could see keeping it; its player says so, and what is in the pool lasts at least that long.
                colors = set(str(op.get("colors") or "WUBRGC"))
                for floating in player.pool:
                    if floating[0] in colors and mana.LIFETIMES.index(floating[1]) < mana.LIFETIMES.index(keep):
                        floating[1] = keep
            player.pool += [[color, until] for color in added]
            holds = "".join(f"{{{m[0]}}}" for m in player.pool) or "nothing"
            said = (f"adds {''.join('{' + c + '}' for c in added)}" + ("" if until == "step" else f" (until {until.replace('_', ' ')})") if added
                    else f"removes {''.join('{' + c + '}' for c in removed)}" if removed else f"keeps the mana in their pool (until {str(keep).replace('_', ' ')})")
            told.public.append(f"{player.name} {said}{because}; their mana pool holds {holds}.")
        case "delay":
            # A delayed triggered ability belongs to no permanent ("return it at the beginning of the next end
            # step", after the card that said so has left), so no hook can carry it. In the fifth pod Norin the
            # Wary's return was forgotten on three turns out of three.
            step, text = str(op.get("step", "")), str(op.get("text", "")).strip()
            if step not in ("upkeep", "draw", "main1", "begin_combat", "end_combat", "main2", "end") or not text:
                raise TableError("bad_delay", "A delayed trigger needs `text` and a `step`: upkeep, draw, main1, begin_combat, end_combat, main2 or end.")
            whose = actor if op.get("whose") == "mine" else _seat(game, op["whose"]) if isinstance(op.get("whose"), int) else "any"
            game.ledger.append({"kind": "delayed", "step": step, "whose": whose, "controller": actor, "text": text, "source": None})
            told.public.append(f"{me} will have a trigger at the beginning of the next {step} step"
                               + ("" if whose == "any" else f" of {_who(game, whose)}'s turn") + f": {text}")
        case "annotate":
            obj = _selected(game, dice, actor, op)[0]
            obj.annotations.append({"text": str(op.get("text", "")), "expires": op.get("expires")})
            told.public.append(f"{_named(obj)}: {op.get('text', '')}"
                               + (f" (until {op['expires']})" if op.get("expires") else "") + ".")
        case "clear_annotations":
            obj = _selected(game, dice, actor, op)[0]
            obj.annotations = []
            told.public.append(f"{me} clears the notes on {_named(obj)}.")
        case "set_controller":
            obj = _selected(game, dice, actor, op)[0]
            obj.controller = _seat(game, op.get("controller"))
            obj.entered_turn = game.turn.number  # it has not been under this control since the turn began
            told.public.append(f"{_who(game, obj.controller)} now controls {_named(obj)}{because}.")
        case "face":
            obj = _selected(game, dice, actor, op)[0]
            if "face_down" in op:
                obj.face_down = bool(op["face_down"])
                obj.public = not obj.face_down
                obj.known_by = set() if obj.public else {obj.controller}
            if "transformed" in op:
                obj.transformed = bool(op["transformed"])
            told.public.append(f"{me} turns {obj.name if obj.public else 'a permanent'} "
                               f"{'face down' if obj.face_down else 'to its other face' if obj.transformed else 'face up'}.")
        case "phase":
            obj = _selected(game, dice, actor, op)[0]
            obj.phased_out = bool(op.get("out", True))
            told.public.append(f"{_named(obj)} phases {'out' if obj.phased_out else 'in'}.")
        case "damage":
            obj = _selected(game, dice, actor, op)[0]
            obj.damage += _count(op, "amount")
            game.facts.append(triggers.fact("damage", name="A source", controller=None, to="permanent", target_id=obj.id,
                                            amount=_count(op, "amount"), combat=False))
            told.public.append(f"{_named(obj)} has {obj.damage} damage marked on it{because}.")
        case "draw":
            seat = _seat(game, op.get("seat", actor))
            drawn = mutations.draw(game, seat, _count(op))
            told.public.append(f"{_who(game, seat)} draws {len(drawn)} card(s){because}."
                               + (" Their library is empty." if game.seats[seat].drew_from_empty else ""))
            told.show += [o.id for o in drawn if seat == actor]
        case "mill":
            seat = _seat(game, op.get("seat", actor))
            milled = [mutations.move(game, game.objects[i], "graveyard")
                      for i in list(game.seats[seat].library[:_count(op)])]
            told.public.append(f"{_who(game, seat)} mills {', '.join(o.name for o in milled) or 'nothing'}{because}.")
        case "shuffle":
            seat = _seat(game, op.get("seat", actor))
            mutations.shuffle(game, seat, dice)
            told.public.append(f"{_who(game, seat)}'s library is shuffled.")
        case "look" | "reveal":
            if "from" not in op and "obj" not in op and "objs" not in op:  # a whole hand
                objs = [game.objects[i] for i in game.seats[_seat(game, op.get("seat"))].hand]
                if op.get("seat") != actor and not op.get("because"):
                    raise TableError("because_required", "Looking at another player's hand needs a `because`.")
            else:
                objs = _selected(game, dice, actor, op)
            viewers = range(len(game.seats)) if kind == "reveal" else [_seat(game, s) for s in op.get("viewers", [actor])]
            place = _where(game, objs[0]) if objs else "an empty zone"
            for obj in objs:
                obj.public = obj.public or kind == "reveal"
                obj.known_by |= set(viewers) if not obj.public else set()
            names = ", ".join(o.name for o in objs) or "nothing"
            if kind == "reveal":
                told.public.append(f"{me} reveals {names} from {place}{because}.")
            else:
                told.public.append(f"{', '.join(_who(game, s) for s in viewers)} look(s) at {len(objs)} card(s) "
                                   f"in {place}{because}.")
                told.secretly(viewers, f"The card(s) in {place}: {names}.")
            told.show += [o.id for o in objs if o.face_known_to(actor)]
        case "discard_random":
            seat = _seat(game, op.get("seat", actor))
            picked = mutations.from_position(game, {"seat": seat, "zone": "hand", "position": "random",
                                                    "count": _count(op)}, dice)
            gone = [mutations.move(game, obj, "graveyard") for obj in picked]
            told.public.append(f"{_who(game, seat)} discards {', '.join(o.name for o in gone) or 'nothing'} at random{because}.")
        case "roll":
            sides = _count(op, "sides", 6)
            rolls = [dice.pick(max(sides, 1)) + 1 for _ in range(_count(op))]
            told.values.append({"roll": rolls, "sides": sides})
            told.public.append(f"{me} rolls d{sides}: {', '.join(map(str, rolls))}{because}.")
        case "flip_coin":
            flips = ["heads" if dice.pick(2) else "tails" for _ in range(_count(op))]
            told.values.append({"flip": flips})
            told.public.append(f"{me} flips: {', '.join(flips)}{because}.")
        case "designate":
            holder = None if op.get("seat") is None else _seat(game, op["seat"])
            game.designations[str(op.get("key", ""))] = holder
            told.public.append(f"{op.get('key')}: {_who(game, holder) if holder is not None else 'nobody'}.")
        case "commander_damage":
            player, source = game.seats[_seat(game, op.get("seat"))], mutations.get(game, op["commander"])
            key = f"{source.name} ({_who(game, source.owner)})"
            player.commander_damage[key] = _count(op, "set") if "set" in op else \
                player.commander_damage.get(key, 0) + _count(op, "amount")
            told.public.append(f"{player.name} has taken {player.commander_damage[key]} commander damage from {key}.")
        case "eliminate":
            seat = _seat(game, op.get("seat"))
            loser = game.seats[seat]
            if not by_table and game.gated and game.mechanics >= 6 and (
                    loser.life <= 0 or loser.counters.get("poison", 0) >= 10 or loser.drew_from_empty or any(n >= 21 for n in loser.commander_damage.values())):
                # Nothing brings a player back, so the driver has the turn so far audited before it removes one. In a whole game
                # it never got the chance: each player at 0 life was removed by hand, in the same wake as the damage.
                raise TableError("table_does_it", f"{loser.name} has lost by a state-based action, which the table sees for itself: it removes them "
                                                  "as soon as anyone would get priority, once the turn so far has been checked. Leave this out "
                                                  "and finish what you are doing.")
            mutations.eliminate(game, seat)
            told.public.append(f"{_who(game, seat)} is out of the game: {op.get('reason', 'no reason given')}.")
        case "extra_turn":
            game.turn.extra_turns.append(_seat(game, op.get("seat", actor)))
            told.public.append(f"{_who(game, game.turn.extra_turns[-1])} will take an extra turn{because}.")
        case "extra_combat":
            mutations.insert_combat(game)
            told.public.append(f"There will be an additional combat phase{because}.")
        case "end_turn":
            mutations.end_turn(game)
            told.public.append(f"{me} ends the turn{because}.")
        case "skip_step":
            mutations.skip_step(game, op.get("step", ""))
            told.public.append(f"The {op.get('step')} step will be skipped{because}.")
        case "remove_from_stack":
            before = len(game.stack)
            game.stack[:] = [item for item in game.stack if item.id != op.get("item")]
            if len(game.stack) == before:
                raise TableError("gone", "That stack item is no longer on the stack.")
            told.public.append(f"{me} removes an ability from the stack{because}.")
        case "request_choice":
            request = turn.request_choice(game, actor, [_seat(game, s) for s in op.get("seats", [])],
                                          str(op.get("prompt", "")), op.get("options"))
            told.public.append(f"{me} asks {', '.join(_who(game, s) for s in request['waiting'])}: {request['prompt']}")
        case _:
            raise TableError("unknown_op", f"Unknown mutation {kind!r}. See the tool description for the list.")


_OPS = frozenset({"annotate", "attach", "clear_annotations", "commander_damage", "counters", "create_token", "damage", "delay", "designate", "mana",
                  "discard_random", "draw", "eliminate", "end_turn", "extra_combat", "extra_turn", "face", "flip_coin", "life", "look",
                  "mill", "move", "phase", "remove_from_stack", "request_choice", "reveal", "roll", "set_controller", "shuffle",
                  "skip_step", "tap", "untap"})


# What each mutation reads, besides `op` and `because`. A key outside it was being dropped without a word: in the 72nd game Zuko
# wrote firebending mana as {"mana": {"add": "RRRRR", "expires": "end_combat"}} (annotate's key), the pool took it for this step's
# mana, and five red emptied before blockers were declared.
_SELECTED = ("obj", "objs", "from")
_FIELDS = {
    "move": (*_SELECTED, "to", "position", "controller", "enters", "face_down", "reveal", "known_by", "stay"),
    "tap": _SELECTED, "untap": _SELECTED, "clear_annotations": _SELECTED,
    "counters": (*_SELECTED, "seat", "kind", "delta", "set"),
    "life": ("seat", "delta", "set"),
    "create_token": ("token", "count", "controller", "enters", "copy_of"),
    "attach": (*_SELECTED, "host"),
    "mana": ("add", "remove", "until", "keep", "colors", "seat"),
    "delay": ("step", "text", "whose"),
    "annotate": (*_SELECTED, "text", "expires"),
    "set_controller": (*_SELECTED, "controller"),
    "face": (*_SELECTED, "face_down", "transformed"),
    "phase": (*_SELECTED, "out"),
    "damage": (*_SELECTED, "amount"),
    "draw": ("seat", "count"), "mill": ("seat", "count"), "discard_random": ("seat", "count"), "shuffle": ("seat",),
    "look": (*_SELECTED, "seat", "viewers"), "reveal": (*_SELECTED, "seat", "viewers"),
    "roll": ("sides", "count"), "flip_coin": ("count",),
    "designate": ("key", "seat"),
    "commander_damage": ("seat", "commander", "amount", "set"),
    "eliminate": ("seat", "reason"),
    "extra_turn": ("seat",), "extra_combat": (), "end_turn": (), "skip_step": ("step",),
    "remove_from_stack": ("item",),
    "request_choice": ("seats", "prompt", "options"),
}


def _unread(op: dict) -> None:
    fields = _FIELDS.get(op.get("op"))
    stray = [key for key in op if fields is not None and key not in fields and key not in ("op", "because")]
    if stray:
        raise TableError("unknown_field", f"`{op['op']}` has no {', '.join(f'`{k}`' for k in stray)}, and the table would have "
                                          f"ignored {'it' if len(stray) == 1 else 'them'}. It takes: "
                                          + (", ".join(fields) or "nothing") + ", and `because`. Nothing was applied.")


def _shaped(op: dict) -> dict:
    """Accept the mutation's name as a key, which agents write again and again, where it has only one
    possible meaning: {"tap": {...}}, {"mill": {}, "seat": 0, "count": 1}, {"move": true, "from": ..., "to": ...}.
    The fourth pod lost 22 calls to it. A key that is also a field of the mutation (a "counters" map
    under `enters`, say) is not at the top level, so nothing is mistaken."""
    if "op" in op:
        # {"op": "move", "move": {"obj": ...}, ...}: said twice, and meant once. What is at the top level stands.
        inner = op.get(op["op"])
        return {**inner, **{k: v for k, v in op.items() if k != op["op"]}} if isinstance(inner, dict) else op
    named = [key for key in op if key in _OPS]
    if len(named) != 1:
        return op
    inner = op[named[0]] if isinstance(op[named[0]], dict) else {}
    return {"op": named[0], **inner, **{k: v for k, v in op.items() if k != named[0]}}


def apply_batch(game: Game, dice: Dice, actor: int, batch: list[dict], *, by_table: bool = False, replaying: bool = False) -> Told:
    """`by_table` is True for the driver's own repairs, which are made in the active player's name but are nobody's play.
    `replaying` is True while a journal is rebuilt: what it holds was accepted once, and a refusal added since must let it by."""
    if not isinstance(batch, list) or not batch or not all(isinstance(op, dict) for op in batch):
        raise TableError("bad_batch", "Give a non-empty list of mutations, each like {\"op\": \"tap\", \"objs\": [...]}.")
    told = Told()
    batch = [_shaped(op) for op in batch]
    for op in batch if not (by_table or replaying) else ():
        _unread(op)
    for index, op in enumerate(batch):
        if op.get("op") == "request_choice" and index != len(batch) - 1:
            raise TableError("bad_batch", "`request_choice` must be the last mutation in its batch: nothing "
                                          "after it can happen until the answers are in.")
        apply_op(game, dice, actor, op, told, by_table=by_table or replaying)
    return told
