"""The table: one game's state, the calls seats may make on it, and an append-only event log.

Every accepted call is applied to a copy of the state and swapped in whole, so a rejected
call changes nothing. Events store calls with handles already resolved, plus every random
outcome, so replaying the log rebuilds the same game.
"""

from __future__ import annotations

import copy
from collections.abc import Callable

from . import mana, mechanics, mutations, ops, scripts, triggers, turn
from .model import MECHANICS, STEPS, Dice, Game, Obj, Seat, StackItem, TableError
from .views import Handles, obj_view, view

_OBJECT_KEYS = frozenset({"card", "source", "obj", "objs", "attacker", "blocker", "attached_to", "copy_of",
                          "commander", "host", "bottom", "tap"})
_OTHER_KEYS = {"item": "s", "pending": "t", "request": "q"}
_RENAMED = {"object": "obj", "stack_item": "item"}
# Which kinds of control let a seat call which tools. Tools not listed need no pen.
_ALLOWED = {
    "mulligan": ("setup",), "keep": ("setup",),
    "pass_priority": ("priority",), "play_land": ("priority",), "cast": ("priority",),
    "activate": ("priority", "resolution", "held_step"), "act": ("priority", "held_step", "turn_action", "correction"),
    "add_trigger": ("priority", "triggers"), "skip_trigger": ("triggers",),
    "resolve": ("resolution",), "finish_resolution": ("resolution",),
    "answer": ("request",), "finish_step": ("held_step", "turn_action", "correction"),
    "declare_attackers": ("turn_action",), "declare_blockers": ("turn_action",), "combat_damage": ("turn_action",),
    "contest": ("correction",),
}
_DISPUTES = 3  # contests and disputes a seat may lose before it may raise no more
_READ_ONLY = frozenset({"get_view", "get_log", "get_notes"})
# What a seat may still do once its wake has ended: nothing here touches the game.
_AFTER_WAKE = frozenset({"write_notes", "get_notes", "set_response_policy", "get_view", "get_log"})
# Calls that change nothing an auditor could judge. Every other seat call marks the table as unaudited.
_NOT_AUDITABLE = frozenset({"pass_priority", "say", "write_notes", "mulligan", "keep", "concede", "answer", "finish_step",
                            "set_response_policy", "contest"})


class Table:
    def __init__(self, seats: list[dict], *, seed: int | None = None, first: int | None = None, gated: bool | str = False,
                 card_lookup: Callable[[str], dict | None] | None = None, on_event: Callable[[dict], None] | None = None,
                 mechanics: int | None = None, _setup_random: list | None = None):
        """`seats` is a list of {name, commanders: [card names], library: [card names]}.

        With `gated`, the table stops at an audit checkpoint whenever something auditable has
        happened and the pen would move on or a mechanical step would run; the driver calls
        `release` when it is satisfied. With `gated="turn"` it stops only between turns, which is all a
        driver that audits whole turns needs: every other stop is journalled and released for nothing. `on_event` receives each event as it is committed, so a
        journal on disk never lags the game."""
        self.config = {"seats": copy.deepcopy(seats), "seed": seed, "first": first, "gated": gated,
                       "mechanics": MECHANICS if mechanics is None else mechanics}
        self.card_lookup, self.gated, self.on_event = card_lookup, gated, on_event
        self.dice = Dice(seed)
        self.events: list[dict] = []
        self.rev = 0
        self.notes = {index: "" for index in range(len(seats))}
        self.handles: dict[int | None, Handles] = {viewer: Handles() for viewer in (None, *range(len(seats)))}
        self._wakes: dict[str, dict] = {}
        self._replaying = False  # a journal holds calls that were accepted: checks added since must not refuse them
        game = Game(seats=[Seat(index, seat["name"]) for index, seat in enumerate(seats)], gated=gated, mechanics=self.config["mechanics"])
        for index, seat in enumerate(seats):
            for zone, names, commander in (("command", seat.get("commanders", []), True),
                                           ("library", seat.get("library", []), False)):
                for name in names:
                    obj = Obj(id=game.new_id(), uid=0, owner=index, controller=index, zone=zone, name=name,
                              is_commander=commander, public=commander,
                              type_line=str(((card_lookup(name) if card_lookup else None) or {}).get("type_line", "")))
                    obj.uid = obj.id
                    game.objects[obj.id] = obj
                    getattr(game.seats[index], zone).append(obj.id)
        game._lookup = card_lookup  # not a field: what the table was told about cards, for the rules it applies itself
        self.game = game
        self._commit(None, "setup", {}, lambda g: turn.start_game(g, self.dice, first), _setup_random)

    # ---- the call contract -------------------------------------------------------------------

    def open_wake(self, seat: int, kind: str = "priority") -> str:
        token = f"w{len(self._wakes) + 1}"
        self._wakes[token] = {"seat": seat, "kind": kind, "alive": True, "calls": {}}
        return token

    def close_wake(self, token: str) -> None:
        if token in self._wakes:
            self._wakes[token]["alive"] = False

    def call(self, token: str, call_id: str, tool: str, args: dict | None = None) -> dict:
        """A call made inside a wake. A dead wake cannot act, and a repeated call id returns the
        first result instead of acting twice."""
        wake = self._wakes.get(token)
        if wake is not None and not wake["alive"] and tool in _AFTER_WAKE:
            return self.act(wake["seat"], tool, args)  # a player's last act is often to save its notes
        if wake is None or not wake["alive"]:
            return TableError("stale_wake", "This wake has ended or the table was corrected under it. "
                                            "Stop; you will be woken again if you are needed.").to_dict()
        if call_id not in wake["calls"]:
            wake["calls"][call_id] = self.act(wake["seat"], tool, args)
        return wake["calls"][call_id]

    def expected(self) -> list[tuple[int, str]]:
        """The seats the table is waiting for, and the kind of wake each needs."""
        control = self.game.control
        kind = control.detail.get("action", control.kind)
        return [(seat, "mulligan" if kind == "setup" else kind) for seat in control.seats]

    # ---- seat calls --------------------------------------------------------------------------

    def act(self, seat: int, tool: str, args: dict | None = None) -> dict:
        """One call from a seat. Returns a result, or {"error": {code, message}}; never raises for
        anything an agent can get wrong."""
        try:
            if not 0 <= seat < len(self.game.seats):
                raise TableError("bad_seat", "No such seat.")
            args = dict(args or {})
            if tool in _READ_ONLY:
                return getattr(self, f"_{tool}")(seat, args)
            if self.game.seats[seat].status != "alive":
                raise TableError("not_playing", "You are out of this game.")
            return self._execute(seat, tool, self._resolve(seat, args))
        except TableError as error:
            return error.to_dict()
        except (KeyError, TypeError, ValueError, IndexError) as error:
            return TableError("bad_arguments", f"Could not read the arguments to `{tool}`: {error!r}. "
                                               "Check the tool description.").to_dict()

    def view(self, viewer: int | None) -> dict:
        result = view(self.game, viewer, self.handles[viewer], self.rev)
        result["warnings"] = self._state_warnings()
        return result

    def _get_view(self, seat: int, args: dict) -> dict:
        return self.view(seat)

    def _get_log(self, seat: int | None, args: dict) -> dict:
        since = int(args.get("since", 0))
        shown: list[dict] = []
        for e in self.events[since:]:
            if not (e["public"] or str(seat) in e["private"] or e["lints"]):
                continue  # an event with nothing for this viewer is not shown, tool name included
            entry = {"n": e["n"], "seat": e["seat"], "tool": e["tool"], "turn": e["turn"], "step": e["step"], "text": e["public"],
                     **({"private": e["private"][str(seat)]} if str(seat) in e["private"] else {}),
                     **({"lints": e["lints"]} if e["lints"] else {})}
            plain = e["tool"] == "pass_priority" and "private" not in entry and "lints" not in entry and len(e["public"]) <= 1
            if plain and shown and shown[-1].get("passes") and shown[-1]["turn"] == e["turn"]:
                # A run of passes is one entry: who passed matters now and then, a line each never does.
                run = shown[-1]
                run["to"], run["passes"] = e["n"], run["passes"] + 1
                name = self._name(e["seat"])
                run["who"] += [] if name in run["who"] else [name]
                run["text"] = [f"{', '.join(run['who'])} passed priority."]
            elif plain:
                shown.append({**entry, "seat": None, "to": e["n"], "passes": 1, "who": [self._name(e["seat"])],
                              "text": [f"{self._name(e['seat'])} passed priority."]})
            else:
                shown.append(entry)
        return {"events": shown, "next": len(self.events)}

    def _get_notes(self, seat: int, args: dict) -> dict:
        return {"notes": self.notes[seat]}

    def _resolve(self, seat: int, value, key: str | None = None):
        """Replace the caller's handles with internal ids, refusing anything they cannot point at."""
        if isinstance(value, dict):
            return {_RENAMED.get(k, k): self._resolve(seat, v, _RENAMED.get(k, k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(seat, v, key) for v in value]
        if value is None or isinstance(value, bool) or (key not in _OBJECT_KEYS and key not in _OTHER_KEYS):
            return value  # `tap: false` on an attacker is a flag; `tap: [handles]` in a payment is a list of cards
        kind = "c" if key in _OBJECT_KEYS else _OTHER_KEYS[key]
        internal = self.handles[seat].resolve(kind, value)
        if internal is None:
            raise TableError("unknown_handle", f"{value!r} is not one of your handles for `{key}`. Handles come "
                                               "from get_view and change when a card changes zones.")
        if kind == "c":
            obj = self.game.objects.get(internal)
            if obj is None or not obj.addressable_by(seat):
                raise TableError("gone", f"{value!r} is no longer there: the card changed zones or was shuffled "
                                         "away. Call get_view for current handles.")
        return internal

    # ---- execution, shared by live calls and replay ------------------------------------------

    def _execute(self, seat: int | None, tool: str, args: dict, recorded: list | None = None) -> dict:
        handler = getattr(self, f"_do_{tool}", None)
        if handler is None:
            raise TableError("unknown_tool", f"There is no tool called {tool!r}.")

        def run(game: Game):
            if tool in _ALLOWED:
                control = game.control
                if seat not in control.seats or control.kind not in _ALLOWED[tool]:
                    holders = ", ".join(game.seats[s].name for s in control.seats) or "nobody"
                    raise TableError("not_your_pen", f"`{tool}` is not available to you now. The table is waiting "
                                                     f"for {holders} ({control.detail.get('action', control.kind)}).")
            if self.gated and seat is not None and tool not in _NOT_AUDITABLE:
                game.audit_pending = True
            return handler(game, seat, args)

        return self._commit(seat, tool, args, run, recorded)

    def _commit(self, seat: int | None, tool: str, args: dict, run: Callable, recorded: list | None = None) -> dict:
        game, before = copy.deepcopy(self.game), self.game.turn
        self.dice.begin(recorded)
        try:
            outcome = run(game) or ops.Told()
        except Exception:
            self.dice.rollback()
            raise
        told = outcome if isinstance(outcome, ops.Told) else outcome[0]
        lints = [] if isinstance(outcome, ops.Told) else outcome[1]
        told.public += game.announcements
        game.announcements = []
        for whispered_to, sentence in game.whispers:
            told.secretly([whispered_to], sentence)
        game.whispers = []
        turn.repair_after_elimination(game)
        game.ledger[:] = [e for e in game.ledger if e.get("source") is None or e["source"] in game.objects]
        self.game, self.rev = game, self.rev + 1
        event = {"n": len(self.events), "rev": self.rev, "seat": seat, "tool": tool, "args": args,
                 "turn": before.number, "step": before.step,  # when the call was made, not where it led
                 "round": self.game.seats[before.active].turns_taken if before.number else 0,
                 "random": list(self.dice.recorded), "public": told.public,
                 "private": {str(s): lines for s, lines in told.private.items()}, "lints": lints}
        self.events.append(event)
        if self.on_event:
            self.on_event(event)
        control = game.control
        result = {"ok": True, "rev": self.rev, "log": told.public,
                  "pen": {"kind": control.detail.get("action", control.kind), "seats": control.seats}}
        if seat is not None:
            shown = [game.objects[i] for i in told.show if i in game.objects and game.objects[i].face_known_to(seat)]
            for key, value in (("private", told.private.get(seat)), ("values", told.values), ("lints", lints),
                               ("shown", [obj_view(game, o, seat, self.handles[seat]) for o in shown])):
                if value:
                    result[key] = value
        return result

    def _state_warnings(self) -> list[str]:
        """Player-level conditions the rules would act on. The table only points them out."""
        warnings = []
        for seat in self.game.seats:
            if seat.status != "alive":
                continue
            if seat.life <= 0:
                warnings.append(f"{seat.name} is at {seat.life} life and still in the game.")
            if seat.counters.get("poison", 0) >= 10:
                warnings.append(f"{seat.name} has {seat.counters['poison']} poison counters.")
            if seat.drew_from_empty:
                warnings.append(f"{seat.name} tried to draw from an empty library.")
            warnings += [f"{seat.name} has taken {n} commander damage from {source}."
                         for source, n in seat.commander_damage.items() if n >= 21]
        return warnings

    # ---- handlers: each takes the working copy, the seat and resolved arguments --------------

    def _name(self, seat: int) -> str:
        return self.game.seats[seat].name

    def _own(self, game: Game, seat: int, obj_id: int, what: str) -> Obj:
        obj = mutations.get(game, obj_id)
        if obj.zone != "battlefield" or obj.controller != seat:
            raise TableError("not_yours", f"You can only {what} with a permanent you control.")
        return obj

    def _targets_text(self, game: Game, targets: list[dict]) -> str:
        names = []
        for target in targets:
            if "seat" in target:
                names.append(game.seats[target["seat"]].name)
            elif "item" in target:
                names.append(next((i.source_name for i in game.stack if i.id == target["item"]), "a spell"))
            else:
                obj = mutations.get(game, target["obj"])
                names.append(f"{ops._named(obj)} ({game.seats[obj.controller].name})")
        return f" targeting {', '.join(names)}" if names else ""

    def _pay(self, game: Game, seat: int, payment: dict | None) -> str:
        payment = payment or {}
        tapped = [self._own(game, seat, i, "pay") for i in payment.get("tap", [])]
        for obj in tapped:
            if obj.tapped:
                raise TableError("already_tapped", f"{obj.name} is already tapped.")
            obj.tapped = True
        parts = ([f"tapping {', '.join(ops._named(o) for o in tapped)}"] if tapped else []) + \
                ([str(payment["note"])] if payment.get("note") else [])
        return "; ".join(parts)

    def _do_mulligan(self, game: Game, seat: int, args: dict):
        turn.mulligan(game, seat, self.dice)
        told = ops.Told(public=[f"{self._name(seat)} mulligans (number {game.seats[seat].mulligans})."])
        told.show = list(game.seats[seat].hand)
        return told

    def _do_keep(self, game: Game, seat: int, args: dict):
        turn.keep(game, seat, args.get("bottom", []))
        return ops.Told(public=[f"{self._name(seat)} keeps {len(game.seats[seat].hand)} cards."])

    def _do_pass_priority(self, game: Game, seat: int, args: dict):
        until = args.get("until")
        if until is not None:
            if until not in STEPS or STEPS.index(until) <= STEPS.index(game.turn.step):
                raise TableError("bad_step", f"`until` must be a later step of this turn: one of "
                                             f"{', '.join(STEPS[STEPS.index(game.turn.step) + 1:])}.")
            game.seats[seat].yield_to = {"turn": game.turn.number, "step": until}
        turn.pass_priority(game, seat)
        return ops.Told(public=[f"{self._name(seat)} passes priority" + (f", and will keep passing until the {until} step "
                                "unless something happens." if until else ".")])

    def _do_play_land(self, game: Game, seat: int, args: dict):
        obj = mutations.get(game, args["card"])
        lints = []
        if game.turn.land_plays >= 1:
            lints.append(f"This is land number {game.turn.land_plays + 1} this turn.")
        if self._replaying and (game.turn.active != seat or game.turn.step not in ("main1", "main2") or game.stack):
            lints.append("A land is normally played in your own main phase with an empty stack.")
        elif game.turn.active != seat or game.turn.step not in ("main1", "main2") or game.stack:
            # The table knows the turn, so this much of rule 305 it can hold: no effect lets a land be
            # played, as a land play, at another time. An effect that puts a land onto the battlefield is a move.
            raise TableError("not_now", "A land can be played only in your own main phase while the stack is empty (rule 305.1). "
                             + (f"It is your {game.turn.step} step: pass priority until \"main1\"." if game.turn.active == seat and not game.stack
                                else "If an effect is putting this land onto the battlefield, move it there as part of resolving that effect."))
        origin, because = ops._where(game, obj), str(args.get("because", "")).strip()
        if not (obj.owner == seat and obj.zone == "hand") and not because and not self._replaying:
            raise TableError("say_why", f"{obj.name} is in {origin}, not in your hand. If an effect lets you play it from there, "
                                        "call play_land again with `because` naming that effect.")
        land = mutations.move(game, obj, "battlefield", controller=seat, enters=args.get("enters"))
        game.turn.land_plays += 1
        turn.acted(game)
        told = ops.Told(public=[f"{self._name(seat)} plays {land.name} from {origin}" + (f" (allowed by: {because})" if because else "") + "."],
                        show=[land.id])
        return told, lints

    def _do_cast(self, game: Game, seat: int, args: dict):
        obj = mutations.get(game, args["card"])
        face_down = bool(args.get("face_down"))
        name, origin, from_command = obj.name, ops._where(game, obj), obj.zone == "command"
        usual = obj.owner == seat and obj.zone in ("hand", "command")
        because = str(args.get("because", "")).strip()
        if not usual and not because and not self._replaying:
            # A slip of a handle, or a real permission (flashback, a card exiled "you may play it", an
            # opponent's card): only the player knows, so it has to say. In the second pod a player cast
            # another player's exiled card by mistake and had to undo it by hand.
            raise TableError("say_why", f"{name} is in {origin}, not in your hand or your command zone. If an effect lets you cast it "
                                        "from there, call cast again with `because` naming that effect.")
        info = self.card_lookup(name) if self.card_lookup else None
        main_phase = game.turn.active == seat and game.turn.step in ("main1", "main2") and not game.stack
        if info and info.get("types") and not main_phase and not because and not self._replaying and not face_down \
                and "Instant" not in info["types"] and "Flash" not in info.get("keywords", []):
            # The table is told a card's types, so this much of the timing rules it can ask about. In
            # the second pod a creature was cast in the end of combat step.
            raise TableError("say_why", f"{name} is not an instant and has no flash, and this is not your main phase with an empty stack "
                                        f"(it is the {game.turn.step} step of {game.seats[game.turn.active].name}'s turn"
                                        + (", with something on the stack" if game.stack else "") + "). If an effect lets you cast it "
                                        "now, call cast again with `because` naming that effect.")
        payment = args.get("payment") or {}
        if info and payment.get("tap") and not payment.get("note") and not self._replaying and not face_down:
            tax = 2 * game.seats[seat].commander_casts.get(name, 0) if from_command else 0
            problem = mana.payment_problem(game, seat, info, payment["tap"], x=int(args.get("x") or 0), tax=tax, lookup=self.card_lookup)
            if problem:
                # In the third pod {2}{G}{G} was paid with Swamp, Swamp, Rogue's Passage and a Forest, and the auditor found it a turn later.
                raise TableError("cannot_pay", f"{name}: {problem} Tap sources that can make it; or if something else pays or "
                                               "changes the cost (a Treasure you sacrifice, a cost reducer), say what in the payment's `note`.")
        paid = self._pay(game, seat, args.get("payment"))
        lints = []
        tapped = len((args.get("payment") or {}).get("tap", []))
        if info and tapped and not (args.get("payment") or {}).get("note") and tapped < info.get("mana_value", 0):
            lints.append(f"{name} has mana value {info['mana_value']:g} and {tapped} permanent(s) were tapped for it.")
        if from_command:
            casts = game.seats[seat].commander_casts
            if casts.get(name):
                lints.append(f"Commander tax: {name} costs {{{2 * casts[name]}}} more this time.")
            casts[name] = casts.get(name, 0) + 1
        cast_from = obj.zone
        card = mutations.move(game, obj, "stack", controller=seat, face_down=face_down)
        game.facts.append({"event": "cast", "id": card.id, "old_id": None, "name": name, "type_line": card.type_line, "from": cast_from,
                           "controller": seat, "was_controller": seat, "token": False})
        item = StackItem(id=game.new_id(), kind="spell", controller=seat,
                         source_name="a face-down spell" if face_down else name, text=str(args.get("text", "")),
                         obj=card.id, targets=args.get("targets", []), x=args.get("x"),
                         modes=args.get("modes", []), payment=paid,
                         auto_permanent=args.get("resolves") == "auto_permanent")
        game.stack.append(item)
        turn.acted(game)
        told = ops.Told(public=[f"{self._name(seat)} casts {item.source_name} from {origin}"
                                f"{self._targets_text(game, item.targets)}" + (f", {paid}" if paid else "")
                                + (f" (allowed by: {because})" if because else "")
                                + self._declared(item, hide_text=face_down) + "."])
        return told, lints

    @staticmethod
    def _declared(item: StackItem, *, hide_text: bool = False) -> str:
        """X, modes and the announced text, which an auditor needs once the item is off the stack."""
        parts = ([f"X={item.x}"] if item.x is not None else []) + ([f"modes: {', '.join(map(str, item.modes))}"] if item.modes else [])
        parts += [f"announced: \"{item.text}\""] if item.text and not hide_text else []
        return f" [{'; '.join(parts)}]" if parts else ""

    def _do_activate(self, game: Game, seat: int, args: dict):
        source = mutations.get(game, args["source"])
        paid = self._pay(game, seat, args.get("payment"))
        text = str(args.get("ability", ""))
        label = f"{ops._named(source)}: {text}" + (f" ({paid})" if paid else "")
        if game.control.kind != "priority" and not args.get("mana_ability"):
            raise TableError("not_now", "Only a mana ability can be activated while something is resolving.")
        if args.get("mana_ability"):
            return ops.Told(public=[f"{self._name(seat)} activates a mana ability of {label}."])
        item = StackItem(id=game.new_id(), kind="activated", controller=seat, source=source.id,
                         source_name=ops._named(source), text=text, targets=args.get("targets", []),
                         x=args.get("x"), payment=paid)
        game.stack.append(item)
        if game.control.kind == "priority":
            turn.acted(game)
        return ops.Told(public=[f"{self._name(seat)} activates {label}{self._targets_text(game, item.targets)}"
                                + (f" [X={item.x}]" if item.x is not None else "") + "."])

    def _do_add_trigger(self, game: Game, seat: int, args: dict):
        source = mutations.get(game, args["source"]) if args.get("source") is not None else None
        waiting = None
        if game.control.kind == "triggers":
            if args.get("pending") is None:
                raise TableError("pending_required", "Say which waiting trigger this is with `pending`, "
                                                     "or decline it with skip_trigger.")
            waiting = next((t for t in game.pending_triggers if t["id"] == args["pending"]), None)
        text = str(args.get("text") or (waiting or {}).get("text", ""))
        item = StackItem(id=game.new_id(), kind="triggered", controller=seat, source=source.id if source else None,
                         source_name=ops._named(source) if source else "a triggered ability", text=text,
                         targets=args.get("targets", []))
        game.stack.append(item)
        if game.control.kind == "triggers":
            game.turn.passed = []  # something new is on the stack: everyone gets priority again, whoever passed before
            turn.trigger_handled(game, args["pending"], one_of_them=True)
        else:
            # Put on the stack by hand before the table asked: the matching queued trigger is this one.
            turn.collect_triggers(game)
            queued = next((t for t in game.pending_triggers if t["seat"] == seat and source is not None
                           and t.get("source") == source.id), None)
            if queued:
                game.pending_triggers.remove(queued)
            turn.acted(game)
        return ops.Told(public=[f"{self._name(seat)} puts a trigger on the stack ({item.source_name}): {text}"
                                f"{self._targets_text(game, item.targets)}."])

    def _do_skip_trigger(self, game: Game, seat: int, args: dict):
        trigger = turn.trigger_handled(game, args["pending"])
        return ops.Told(public=[f"{self._name(seat)} does not put a trigger on the stack: {trigger['text']} "
                                f"({args.get('because', 'no reason given')})."])

    def _do_act(self, game: Game, seat: int, args: dict):
        if not args.get("because"):
            raise TableError("because_required", "`act` needs a `because`: the rule or effect you are carrying out.")
        told = ops.apply_batch(game, self.dice, seat, args.get("mutations"))
        told.public.insert(0, f"{self._name(seat)} acts: {args['because']}")
        if game.control.kind == "priority" and seat in game.control.seats:
            turn.acted(game)
        return told

    def _do_resolve(self, game: Game, seat: int, args: dict):
        told = ops.apply_batch(game, self.dice, seat, args.get("mutations"))
        if game.resolution:
            game.resolution["batches"] += 1
        return told

    def _do_finish_resolution(self, game: Game, seat: int, args: dict):
        item = game.resolution["item"]
        turn.finish_resolution(game, args.get("card_to", "graveyard"), args.get("enters"))
        return ops.Told(public=[f"{item.source_name} ({self._name(seat)}) has resolved."])

    def _do_answer(self, game: Game, seat: int, args: dict):
        request = next((r for r in game.requests if r["id"] == args.get("request")), None)
        turn.answer(game, seat, args.get("request"), args.get("answer"))
        return ops.Told(public=[f"{self._name(seat)} answers {request['prompt']!r}: {args.get('answer')!r}"])

    def _do_finish_step(self, game: Game, seat: int, args: dict):
        what = game.control.detail.get("step") or game.control.detail.get("action")
        if game.control.kind == "correction":
            turn.correction_done(game)
            return ops.Told(public=[f"{self._name(seat)} has finished correcting the table."])
        if game.control.kind == "held_step":
            turn.held_step_done(game)
        elif game.control.detail.get("action") == "discard":
            turn.turn_action_done(game)
        else:
            raise TableError("not_finishable", f"Use `{game.control.detail.get('action')}` to complete this step.")
        told = ops.Told(public=[f"{self._name(seat)} has finished the {what} step."])
        over = len(game.seats[seat].hand) - 7
        return told, ([f"{self._name(seat)} ends the turn with {over + 7} cards in hand."] if what == "discard" and over > 0 else [])

    def _do_declare_attackers(self, game: Game, seat: int, args: dict):
        self._expect_action(game, "declare_attackers")
        lints, names = [], []
        for attack in args.get("attacks", []):
            attacker = self._own(game, seat, attack["attacker"], "attack")
            problem = mechanics.attack_problem(game, attacker, self.card_lookup)
            if problem and self.card_lookup and not args.get("because") and not self._replaying:
                raise TableError("say_why", f"{problem} If an effect lets it attack all the same, declare the attack again with `because` "
                                            "naming that effect; nothing has been declared.")
            if attacker.entered_turn == game.turn.number:
                lints.append(f"{ops._named(attacker)} came under your control this turn; it needs haste to attack.")
            if attack.get("tap", True):
                attacker.tapped = True
            game.turn.attackers.append({"attacker": attacker.id, "target": attack["target"]})
            game.facts.append({"event": "attacks", "id": attacker.id, "old_id": None, "name": attacker.name,
                               "type_line": attacker.type_line, "controller": seat, "was_controller": seat,
                               "token": attacker.token is not None})
            names.append(f"{ops._named(attacker)}{self._targets_text(game, [attack['target']]).replace(' targeting', ' at')}")
        turn.turn_action_done(game)
        return ops.Told(public=[f"{self._name(seat)} attacks with {', '.join(names) or 'nothing'}."]), lints

    def _do_declare_blockers(self, game: Game, seat: int, args: dict):
        self._expect_action(game, "declare_blockers")
        attacking, names = {a["attacker"] for a in game.turn.attackers}, []
        for block in args.get("blocks", []):
            blocker = self._own(game, seat, block["blocker"], "block")
            if block["attacker"] not in attacking:
                raise TableError("not_attacking", "You can only block a creature that is attacking.")
            problem = mechanics.block_problem(game, blocker, mutations.get(game, block["attacker"]), self.card_lookup)
            if problem and self.card_lookup and not args.get("because") and not self._replaying:
                raise TableError("say_why", f"{problem} If an effect allows this block all the same, declare your blocks again with "
                                            "`because` naming that effect; nothing has been declared.")
            game.turn.blockers.append({"blocker": blocker.id, "attacker": block["attacker"]})
            game.facts.append(triggers.fact("blocks", name=blocker.name, controller=seat, obj=blocker))
        problem = mechanics.menace_problem(game, args.get("blocks", []), self.card_lookup) if self.card_lookup else None
        if problem and not args.get("because") and not self._replaying:
            raise TableError("say_why", f"{problem} Declare your blocks again with two blockers on it or none; or with `because`, if an "
                                        "effect allows this block. Nothing has been declared.")
            names.append(f"{ops._named(blocker)} blocks {ops._named(mutations.get(game, block['attacker']))}")
        turn.turn_action_done(game)
        return ops.Told(public=[f"{self._name(seat)}: {', '.join(names) or 'no blocks'}."])

    def _do_combat_damage(self, game: Game, seat: int, args: dict):
        self._expect_action(game, "combat_damage")
        told, proposal = ops.Told(), None
        if args.get("as_proposed"):
            proposal = mechanics.propose_damage(game, self.card_lookup)
            if proposal is None:
                raise TableError("no_proposal", "The table cannot work this combat out: it lacks the numbers of a creature in it. "
                                                "Give the assignments yourself.")
            told.public.append("Combat damage as the table worked it out from " + " and ".join(proposal["basis"]) + ":")
            # Written into the recorded call, so that the journal says what was dealt without a reader redoing the sums.
            args.update(assignments=proposal["assignments"], another_step=False, deaths=proposal["deaths"])
        game.turn.damage_steps += 1
        for hit in args.get("assignments", []):
            source, amount = mutations.get(game, hit["source"]), ops._count(hit, "amount")
            if "seat" in hit["target"]:
                player = game.seats[ops._seat(game, hit["target"]["seat"])]
                player.life -= amount
                told.public.append(f"{ops._named(source)} deals {amount} to {player.name} ({player.life} life).")
                if amount:
                    game.facts.append(triggers.fact("damage", name=source.name, controller=source.controller, obj=source, to="player",
                                                    to_player=player.index, amount=amount, combat=True))
                    game.facts.append(triggers.fact("life", name=player.name, controller=player.index, delta=-amount))
                if source.is_commander and amount:
                    ops.apply_op(game, self.dice, seat, {"op": "commander_damage", "seat": player.index,
                                                         "commander": source.id, "amount": amount}, told)
            else:
                victim = mutations.get(game, hit["target"]["obj"])
                victim.damage += amount
                if amount:
                    game.facts.append(triggers.fact("damage", name=source.name, controller=source.controller, obj=source, to="permanent",
                                                    target_id=victim.id, amount=amount, combat=True))
                told.public.append(f"{ops._named(source)} deals {amount} to {ops._named(victim)}.")
        if proposal:
            for who, amount in proposal["gains"].items():
                game.seats[who].life += amount
                game.facts.append(triggers.fact("life", name=game.seats[who].name, controller=who, delta=amount))
                told.public.append(f"{game.seats[who].name} gains {amount} life (lifelink) and is at {game.seats[who].life}.")
            for obj_id in proposal["deaths"]:
                dead = game.objects[obj_id]
                gone = mutations.move(game, dead, "graveyard")
                told.public.append(f"{ops._named(dead)} has lethal damage and dies.")
                if gone is not None and gone.is_commander:  # it did die; its owner may choose the graveyard instead, by hand
                    mutations.move(game, gone, "command")
                    told.public.append(f"{gone.name} is a commander, and goes from the graveyard to the command zone (rule 903.9a).")
            later = mechanics.propose_damage(game, self.card_lookup) if proposal["first_strike_step"] else None
            if later and later["assignments"]:  # somebody is left to deal regular damage after the first-strike step
                game.turn.remaining.insert(0, "combat_damage")
        if args.get("then"):
            followup = ops.apply_batch(game, self.dice, seat, args["then"])
            told.public += followup.public
            told.show += followup.show
        if args.get("another_step"):
            game.turn.remaining.insert(0, "combat_damage")
        if game.control.kind == "turn_action":  # unless a follow-up ended the game or asked a question
            turn.turn_action_done(game)
        return told

    def affordable(self, seat: int) -> list[dict]:
        """The cards in `seat`'s hand and command zone that its untapped mana sources could pay for now,
        each with a payment ready to use, in the seat's own handles: {name, card, tap}."""
        if not self.card_lookup:
            return []
        game, me, handles = self.game, self.game.seats[seat], self.handles[seat]
        held = [(i, 0) for i in me.hand] + [(i, 2 * me.commander_casts.get(game.objects[i].name, 0)) for i in me.command]
        cards = [(game.objects[i].name, self.card_lookup(game.objects[i].name), tax) for i, tax in held]
        by_name = {game.objects[i].name: i for i, _ in reversed(held)}
        return [{"name": name, "card": handles.of("c", by_name[name]), "tap": [handles.of("c", i) for i in tapped]}
                for name, tapped in mana.affordable(game, seat, cards, self.card_lookup)]

    def proposed_damage(self, viewer: int | None) -> dict | None:
        """This combat damage step as the table works it out, in `viewer`'s handles, with a line of
        text for each thing that would happen. None when it cannot, or no damage step is waiting."""
        game = self.game
        if game.control.detail.get("action") != "combat_damage":
            return None
        proposal = mechanics.propose_damage(game, self.card_lookup)
        if proposal is None:
            return None
        handles = self.handles[viewer]

        def name(target: dict) -> str:
            return game.seats[target["seat"]].name if "seat" in target else ops._named(game.objects[target["obj"]])
        lines = [f"{ops._named(game.objects[a['source']])} deals {a['amount']} to {name(a['target'])}" for a in proposal["assignments"]]
        lines += [f"{game.seats[who].name} gains {amount} (lifelink)" for who, amount in proposal["gains"].items()]
        lines += [f"{ops._named(game.objects[i])} dies" for i in proposal["deaths"]]
        return {"lines": lines or ["no damage is dealt"], "first_strike_step": proposal["first_strike_step"], "basis": proposal["basis"],
                "needs_your_choice": [handles.of("c", i) for i in proposal["needs_choice"]]}

    def _expect_action(self, game: Game, action: str) -> None:
        if game.control.detail.get("action") != action:
            raise TableError("wrong_action", f"The table is waiting for `{game.control.detail.get('action')}`.")

    def _do_concede(self, game: Game, seat: int, args: dict):
        mutations.eliminate(game, seat, "conceded")
        return ops.Told(public=[f"{self._name(seat)} concedes."])

    def _do_say(self, game: Game, seat: int, args: dict):
        text = str(args.get("text", "")).strip()
        if not text:
            raise TableError("empty", "Say something.")
        to = [ops._seat(game, s) for s in args.get("to", [])]
        address = f" (to {', '.join(game.seats[s].name for s in to)})" if to else ""
        return ops.Told(public=[f"{self._name(seat)}{address}: {text}"])

    def _may_dispute(self, game: Game, seat: int) -> None:
        if game.seats[seat].failed_disputes >= _DISPUTES:
            raise TableError("no_more_disputes", f"{_DISPUTES} of your contests and disputes have failed; you may raise no more this game.")

    def _do_contest(self, game: Game, seat: int, args: dict):
        reason = str(args.get("reason", "")).strip()
        if not reason:
            raise TableError("empty", "Say why the auditor is wrong: quote the card or the rule.")
        self._may_dispute(game, seat)
        turn.contest(game, seat, reason)
        return ops.Told(public=[f"{self._name(seat)} contests the auditor's finding: {reason} The judge will rule."])

    def _do_dispute(self, game: Game, seat: int, args: dict):
        claim = str(args.get("claim", "")).strip()
        if not claim:
            raise TableError("empty", "Say what was done wrongly, and why.")
        self._may_dispute(game, seat)
        about = args.get("event")
        if about is not None and not (isinstance(about, int) and 0 < about < len(self.events)):
            raise TableError("bad_event", "`event` is an event number from get_log.")
        return ops.Told(public=[f"{self._name(seat)} disputes {f'event {about}' if about is not None else 'what was done'}: {claim} "
                                "The auditor will answer at its next check."])

    def _do_set_response_policy(self, game: Game, seat: int, args: dict):
        policy = args.get("policy")
        if policy not in ("ask", "ask_everything", "pass_unless_targeted", "pass_always"):
            raise TableError("bad_policy", "The policy is \"ask\", \"ask_everything\", \"pass_unless_targeted\" or \"pass_always\".")
        game.seats[seat].response_policy = policy
        told = [f"Until your next turn, when others put something on the stack: {policy.replace('_', ' ')}."]
        if "hold_for_end_step" in args:
            game.seats[seat].holds_for_end_step = bool(args["hold_for_end_step"])
            told.append("You will be given priority in the end step of the turn before yours." if args["hold_for_end_step"]
                        else "You will not be stopped for in the end step before your turn.")
        return ops.Told(private={seat: told})

    def _do_write_notes(self, game: Game, seat: int, args: dict):
        self.notes[seat] = str(args.get("text", ""))
        return ops.Told(public=[f"{self._name(seat)} writes in their notes."], private={seat: ["Notes saved."]})

    # ---- host, auditor and judge actions: not available to seats -----------------------------

    @property
    def awaiting_audit(self) -> bool:
        return self.game.control.kind == "audit"

    def release(self) -> dict:
        """Let the game go on from an audit checkpoint."""
        return self._execute(None, "release", {})

    def upgrade(self) -> dict:
        """Bring a game recorded under older table rules up to the current ones, from this point on. The
        change is an event in the journal, so a replay makes it at the same moment."""
        return self._execute(None, "mechanics", {"to": MECHANICS})

    def _do_mechanics(self, game: Game, seat: None, args: dict):
        game.mechanics = int(args["to"])
        return ops.Told(public=[f"The table's own rules are now version {args['to']}."])

    def _do_release(self, game: Game, seat: None, args: dict):
        turn.release(game)

    def apply_computed(self, computed: list[dict]) -> dict:
        """Stamp permanents with the power and toughness the auditor worked out, replacing every
        earlier stamp. Each is {handle, power, toughness, because}, by the auditor's public handle.
        Rules knowledge the card-blind table cannot have, kept where every player will read it."""
        resolved = []
        for entry in computed:
            obj = self.game.objects.get(self.handles[None].resolve("c", entry.get("handle")) or -1)
            if obj is not None and obj.zone == "battlefield":
                resolved.append({"id": obj.id, "power": str(entry.get("power", "")), "toughness": str(entry.get("toughness", "")),
                                 "because": str(entry.get("because", "")),
                                 **({"keywords": [str(k).lower() for k in entry["keywords"]]} if isinstance(entry.get("keywords"), list) else {})})
        return self._execute(None, "computed", {"computed": resolved})

    def _do_computed(self, game: Game, seat: None, args: dict):
        when = f"turn {game.turn.number}, {game.turn.step}"
        for obj in game.objects.values():
            obj.computed = None
        for entry in args["computed"]:
            if entry["id"] in game.objects:
                game.objects[entry["id"]].computed = {k: entry[k] for k in ("power", "toughness", "because", "keywords") if k in entry} \
                    | {"as_of": when}
        return ops.Told()

    def queue_trigger(self, seat: int, text: str) -> dict:
        """An ability the auditor says has triggered: its controller must deal with it before anyone gets priority."""
        return self._execute(None, "queue_trigger", {"seat": seat, "text": text})

    def _do_queue_trigger(self, game: Game, seat: None, args: dict):
        game.pending_triggers.append({"id": game.new_id(), "seat": ops._seat(game, args["seat"]), "text": str(args["text"]),
                                      "source": None})
        return ops.Told(public=[f"A trigger is waiting for {self._name(args['seat'])}: {args['text']}"])

    def open_correction(self, seat: int, finding: str) -> dict:
        """Give the pen to the seat a finding is about. They fix the table with `act` and end with `finish_step`."""
        return self._execute(None, "open_correction", {"seat": seat, "finding": finding})

    def _do_open_correction(self, game: Game, seat: None, args: dict):
        turn.open_correction(game, ops._seat(game, args["seat"]), str(args["finding"]))
        return ops.Told(public=[f"The auditor asks {self._name(args['seat'])} to correct the table: {args['finding']}"])

    def rule(self, decision: str, explanation: str, *, ruling: str = "", fix: str = "") -> dict:
        """The judge's decision on a contested finding: "uphold", "overrule" or "amend" (a problem, but
        not the auditor's fix). `ruling` is kept for the rest of the game and shown to every player and
        every audit, so the same question is not argued twice."""
        if decision not in ("uphold", "overrule", "amend"):
            raise TableError("bad_decision", "A ruling upholds, overrules or amends.")
        return self._execute(None, "rule", {"decision": decision, "explanation": explanation, "ruling": ruling, "fix": fix})

    def _do_rule(self, game: Game, seat: None, args: dict):
        contesting = game.control.detail.get("seat")
        turn.ruled(game, args["decision"], str(args.get("fix") or ""))
        if args["decision"] == "uphold":
            game.seats[contesting].failed_disputes += 1
        if args.get("ruling"):
            game.rulings.append({"turn": game.turn.number, "text": str(args["ruling"])})
        words = {"uphold": "upholds the auditor's finding", "overrule": "overrules the auditor: nothing is to be corrected",
                 "amend": "amends the auditor's finding"}[args["decision"]]
        return ops.Told(public=[f"The judge {words}. {args['explanation']}"
                                + (f" What {self._name(contesting)} must do: {args['fix']}" if args["decision"] != "overrule" and args.get("fix") else "")
                                + (f" Ruling for the rest of this game: {args['ruling']}" if args.get("ruling") else "")])

    def answer_dispute(self, seat: int, upheld: bool, answer: str) -> dict:
        """The auditor's public answer to a player's dispute. One that fails counts against the seat."""
        return self._execute(None, "answer_dispute", {"seat": seat, "upheld": bool(upheld), "answer": answer})

    def _do_answer_dispute(self, game: Game, seat: None, args: dict):
        who = ops._seat(game, args["seat"])
        if not args["upheld"]:
            game.seats[who].failed_disputes += 1
        return ops.Told(public=[f"The auditor {'agrees with' if args['upheld'] else 'rejects'} {self._name(who)}'s dispute: {args['answer']}"])

    def rewrite_hooks(self, handle: str, hooks: list[dict]) -> dict:
        """Replace everything the ledger holds for one permanent, named by its public handle, with
        `hooks`: for when a better description of the card has been learned since the first was written."""
        source = self.game.objects.get(self.handles[None].resolve("c", handle) or -1)
        if source is None or source.zone != "battlefield":
            raise TableError("bad_hook", f"{handle!r} is not a permanent on the battlefield.")
        return self._execute(None, "hooks", {"replace": source.id, "hooks": [{**{k: v for k, v in h.items() if k != "handle"}, "source_id": source.id}
                                                                          for h in hooks]})

    def apply_hooks(self, hooks: list[dict]) -> dict:
        """Write an auditor's hooks into the ledger. A hook lasts until its source leaves the battlefield.

        A hook names its permanent by the public handle the auditor was shown. A name alone is
        accepted only while exactly one permanent on the battlefield has it (with the hook's
        controller, if it gives one): two players can each have a Howling Mine."""
        resolved = []
        for hook in hooks:
            game, source = self.game, None
            if hook.get("handle"):
                source = game.objects.get(self.handles[None].resolve("c", hook["handle"]) or -1)
                if source is None or source.zone != "battlefield" or (hook.get("source") and source.public
                                                                      and source.name != hook["source"]):
                    raise TableError("bad_hook", f"{hook['handle']!r} is not {hook.get('source') or 'a permanent'} on the battlefield.")
            else:
                named = [game.objects[i] for i in game.battlefield if game.objects[i].public
                         and game.objects[i].name == hook.get("source")
                         and hook.get("controller") in (None, game.objects[i].controller)]
                if len(named) != 1:
                    raise TableError("bad_hook", f"A hook needs the handle of its permanent: {len(named)} permanents "
                                                 f"on the battlefield are called {hook.get('source')!r}.")
                source = named[0]
            resolved.append({**{k: v for k, v in hook.items() if k != "handle"}, "source_id": source.id})
        return self._execute(None, "hooks", {"hooks": resolved})

    def _do_hooks(self, game: Game, seat: None, args: dict):
        told = ops.Told()
        if args.get("replace") is not None:
            game.ledger[:] = [e for e in game.ledger if e.get("source") != args["replace"]]
        elif game.mechanics >= 2:
            # A permanent is described as a whole: what is written for it now replaces what it had. Norin the Wary,
            # described afresh each time it came back, had seven hooks for one ability.
            described = {hook.get("source_id") for hook in args["hooks"]}
            game.ledger[:] = [e for e in game.ledger if e.get("source") not in described]
        for hook in args["hooks"]:
            source = game.objects.get(hook.get("source_id"))
            if hook.get("kind") not in ("hold", "step_trigger", "event_trigger", "static") or source is None:
                raise TableError("bad_hook", f"A hook needs a kind and a source on the battlefield: {hook!r}")
            entry = {"kind": hook["kind"], "text": str(hook.get("text", "")), "source": source.id,
                     "controller": hook.get("controller", source.controller)}
            if hook["kind"] == "event_trigger" and isinstance(hook.get("on"), dict):
                entry["on"] = {k: v for k, v in hook["on"].items() if isinstance(v, (str, int, bool, list))}  # a pattern over facts: see `triggers`
            if hook.get("script") and hook["kind"] in ("event_trigger", "step_trigger") and scripts.problem(hook["script"]) is None:
                # `scripted` is the driver's word that this script has earned its trust; without it the script is only kept.
                entry |= {"script": hook["script"], "scripted": bool(hook.get("scripted"))}
            if hook["kind"] == "hold":
                entry |= {"step": hook.get("step"), "seat": hook.get("seat", source.controller)}
            elif hook["kind"] == "step_trigger":
                entry |= {"step": hook.get("step"), "whose": hook.get("whose", "each")}
            if not any({k: v for k, v in e.items() if k != "text"} == {k: v for k, v in entry.items() if k != "text"}
                       for e in game.ledger):
                game.ledger.append(entry)
                told.public.append(f"Ledger: {source.name}: {entry['text']}")
        return told

    def patch(self, batch: list[dict], because: str, *, _recorded: list | None = None) -> dict:
        """Repair the state going forward (a judge's remedy). Kills every open wake."""
        def run(game: Game):
            told = ops.apply_batch(game, self.dice, game.turn.active, batch)
            told.public.insert(0, f"The judge corrects the table: {because}")
            return told
        result = self._commit(None, "patch", {"mutations": batch, "because": because}, run, _recorded)
        self._kill_wakes()
        return result

    def rewind(self, to_event: int, because: str, *, _recorded: list | None = None) -> dict:
        """Return to the state just after event `to_event`, then reshuffle the part of every
        library nobody knows, since rewound players may have seen cards they would draw again."""
        if not 0 <= to_event < len(self.events):
            raise TableError("bad_event", "There is no such event to rewind to.")
        past = Table.replay(self.config, self.events[: to_event + 1], card_lookup=self.card_lookup)

        def run(game: Game):
            restored = copy.deepcopy(past.game)
            restored.next_id = game.next_id  # ids are never reused, so handles from the abandoned line stay dead
            for seat in restored.seats:
                mutations.shuffle(restored, seat.index, self.dice, only_unknown=True)
            game.__dict__.update(restored.__dict__)
            return ops.Told(public=[f"The table is rewound to just after event {to_event}: {because}"])
        result = self._commit(None, "rewind", {"to": to_event, "because": because}, run, _recorded)
        self.notes = dict(past.notes)
        self._kill_wakes()
        return result

    def _kill_wakes(self) -> None:
        for wake in self._wakes.values():
            wake["alive"] = False

    @classmethod
    def replay(cls, config: dict, events: list[dict], *, card_lookup=None, before=None) -> Table:
        """Rebuild a table from its configuration and event log. `before(table, event)` is called
        ahead of each event, for a reader that needs the table as it stood when the event happened."""
        if not events or events[0]["tool"] != "setup":
            raise TableError("bad_log", "An event log starts with the setup event.")
        table = cls(config["seats"], seed=config["seed"], first=config["first"], gated=config.get("gated", False),
                    card_lookup=card_lookup, mechanics=config.get("mechanics", 0), _setup_random=events[0]["random"])
        table._replaying = True
        for event in events[1:]:
            if before is not None:
                before(table, event)
            if event["tool"] == "patch":
                table.patch(event["args"]["mutations"], event["args"]["because"], _recorded=event["random"])
            elif event["tool"] == "rewind":
                table.rewind(event["args"]["to"], event["args"]["because"], _recorded=event["random"])
            else:
                table._execute(event["seat"], event["tool"], event["args"], event["random"])
        table._replaying = False
        return table
