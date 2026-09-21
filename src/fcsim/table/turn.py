"""The turn skeleton: whose turn, which step, who holds the pen, when the stack resolves.

Nothing here knows a card. Where a step depends on cards (a replaced draw, an upkeep
trigger), a hook in the ledger says so and the step is handed to a seat instead.
"""

from __future__ import annotations

from . import mana, mutations, triggers
from .model import STEPS, Control, Dice, Game, StackItem, TableError

_PRIORITY_STEPS = frozenset(STEPS) - {"untap", "cleanup"}
_HOLDABLE = frozenset({"untap", "draw", "cleanup"})


def start_game(game: Game, dice: Dice, first: int | None = None) -> None:
    for seat in game.seats:
        mutations.shuffle(game, seat.index, dice)
        mutations.draw(game, seat.index, 7)
    game.turn.active = first if first is not None else dice.pick(len(game.seats))
    game.control = Control("setup", seats=[seat.index for seat in game.seats])


def mulligan(game: Game, seat: int, dice: Dice) -> None:
    player = game.seats[seat]
    for obj_id in list(player.hand):
        mutations.move(game, game.objects[obj_id], "library", position="shuffled")
    mutations.shuffle(game, seat, dice)
    mutations.draw(game, seat, 7)
    player.mulligans += 1


def keep(game: Game, seat: int, bottom: list[int]) -> None:
    player = game.seats[seat]
    owed = max(0, player.mulligans - 1)  # the first mulligan is free
    if len(bottom) != owed or len(set(bottom)) != owed or any(i not in player.hand for i in bottom):
        raise TableError("bad_bottom", f"After {player.mulligans} mulligan(s) you must put exactly {owed} "
                                       "different card(s) from your hand on the bottom of your library.")
    for obj_id in bottom:
        mutations.move(game, game.objects[obj_id], "library", position="bottom")
    player.kept = True
    game.control.seats.remove(seat)
    if not game.control.seats:
        begin_turn(game, game.turn.active)


def begin_turn(game: Game, seat: int) -> None:
    turn = game.turn
    turn.number += 1
    game.seats[seat].turns_taken += 1
    game.seats[seat].response_policy = "ask"  # a standing "pass for me" lasts until one's own next turn
    game.seats[seat].holds_for_end_step = False
    turn.active, turn.remaining, turn.passed = seat, list(STEPS), []
    turn.land_plays, turn.attackers, turn.blockers, turn.damage_steps, turn.casts = 0, [], [], 0, {}
    mana.empty_pools(game, entering=None)
    advance(game)


def _next_turn(game: Game) -> None:
    turn = game.turn
    if turn.extra_turns:
        return begin_turn(game, turn.extra_turns.pop())  # the most recently created extra turn is first
    order = game.turn_order_from(turn.active + 1)
    begin_turn(game, order[0])


def _hold(game: Game, step: str) -> dict | None:
    return next((e for e in game.ledger if e.get("kind") == "hold" and e.get("step") == step
                 and e.get("seat") == game.turn.active), None)


def _gate(game: Game, resume: dict) -> bool:
    """The audit checkpoint. If anything auditable has happened since the last one, stop here,
    before the next seat receives priority and before any mechanical step runs, and remember
    what to do when the driver releases the table. The driver decides what to audit and when."""
    if not game.audit_pending or game.gated == "turn":  # "turn": the only checkpoint is the one between turns
        return False
    game.control = Control("audit", [], {"resume": resume})
    return True


def open_correction(game: Game, seat: int, finding: str) -> None:
    """At a checkpoint, hand the pen to the seat an auditor finding is about, to put it right."""
    if game.control.kind != "audit":
        raise TableError("not_waiting", "Corrections are made at an audit checkpoint.")
    game.control = Control("correction", [seat], {"finding": finding, "resume": game.control.detail["resume"]})


def correction_done(game: Game) -> None:
    game.control = Control("audit", [], {"resume": game.control.detail["resume"]})


def contest(game: Game, seat: int, objection: str) -> None:
    """The seat asked to correct the table says the auditor is wrong. Nobody holds the pen until the judge rules."""
    detail = game.control.detail
    if detail.get("final"):
        raise TableError("ruling_final", "The judge has already ruled on this, and the ruling is final. Make the correction and call finish_step.")
    game.control = Control("dispute", [], {"seat": seat, "finding": detail["finding"], "objection": objection, "resume": detail["resume"]})


def ruled(game: Game, decision: str, fix: str) -> None:
    """The judge's decision on a contested finding: "overrule" ends the correction with nothing changed;
    "uphold" and "amend" hand the pen back to the seat, this time with no appeal."""
    if game.control.kind != "dispute":
        raise TableError("not_waiting", "No contested finding is waiting for a ruling.")
    detail = game.control.detail
    if decision == "overrule":
        game.control = Control("audit", [], {"resume": detail["resume"]})
    else:
        finding = fix if decision == "amend" and fix else detail["finding"] + (f" | The judge's instructions: {fix}" if fix else "")
        game.control = Control("correction", [detail["seat"]], {"finding": finding, "resume": detail["resume"], "final": True})


def release(game: Game) -> None:
    if game.control.kind != "audit":
        raise TableError("not_waiting", "The table is not waiting at an audit checkpoint.")
    resume, game.audit_pending = game.control.detail["resume"], False
    if resume["do"] == "next_turn":
        _next_turn(game)
    elif resume["do"] == "advance":
        advance(game)
    elif resume["do"] == "give" and not game.pending_triggers:
        _give_priority(game, resume["seat"])
    else:
        grant_priority(game, resume["seat"])


def advance(game: Game) -> None:
    """Run mechanical steps until some seat has to act."""
    while game.control.kind != "over":
        if _gate(game, {"do": "advance"}):
            return
        turn = game.turn
        if not turn.remaining:
            if game.gated:  # always stop between turns: hooks from this turn must exist before the next untap and upkeep
                game.control = Control("audit", [], {"resume": {"do": "next_turn"}})
                return
            return _next_turn(game)
        step = turn.step = turn.remaining.pop(0)
        turn.passed = []
        mana.empty_pools(game, entering=step)
        if step in _HOLDABLE and (hold := _hold(game, step)):
            game.control = Control("held_step", [turn.active], {"step": step, "because": hold.get("text", "")})
            return
        if _perform(game, step):
            return


def _perform(game: Game, step: str) -> bool:
    """Do a step's mechanical part. True when a seat now holds the pen."""
    turn, active = game.turn, game.turn.active
    if game.seats[active].status != "alive":
        # Rule 800.4j: the turn of a player who has left runs to its end without an active player.
        # They draw, attack and discard nothing; priority goes to the next player in turn order.
        if step == "declare_attackers":
            turn.remaining[:] = [s for s in turn.remaining if s not in ("declare_blockers", "combat_damage")]
        if step == "cleanup":
            return _cleanup(game)
        if step in _PRIORITY_STEPS:
            _queue_step_triggers(game, step)
            grant_priority(game, active)
            return True
        return False
    if step == "begin_combat":
        # Every combat starts clean, an additional one too. The count of damage steps was never reset, so in
        # the fourth pod the table's arithmetic served the first combat of the game and refused all the rest.
        turn.attackers, turn.blockers, turn.damage_steps = [], [], 0
    if step == "untap":
        for obj_id in game.battlefield:
            obj = game.objects[obj_id]
            if obj.controller == active and not obj.phased_out:
                obj.tapped = False
        return False
    if step == "draw":
        mutations.draw(game, active, 1)
    elif step == "declare_attackers":
        if _yielding(game, active):   # passing through combat means attacking with nothing
            turn.remaining[:] = [s for s in turn.remaining if s not in ("declare_blockers", "combat_damage")]
        else:
            game.control = Control("turn_action", [active], {"action": "declare_attackers"})
            return True
    elif step == "declare_blockers":
        defenders = [s for s in game.turn_order_from(active)
                     if any(a["target"].get("seat") == s or _controller_of(game, a["target"]) == s
                            for a in turn.attackers)]
        if defenders:
            game.control = Control("turn_action", defenders[:1], {"action": "declare_blockers",
                                                                  "waiting": defenders[1:]})
            return True
    elif step == "combat_damage":
        game.control = Control("turn_action", [active], {"action": "combat_damage"})
        return True
    elif step == "cleanup":
        return _cleanup(game)
    if step in _PRIORITY_STEPS:
        _queue_step_triggers(game, step)
        grant_priority(game, active)
        return True
    return False


def _controller_of(game: Game, target: dict) -> int | None:
    obj = game.objects.get(target.get("obj", -1))
    return obj.controller if obj else None


def _cleanup(game: Game) -> bool:
    for obj in game.objects.values():
        obj.damage = 0
        obj.annotations = [a for a in obj.annotations if a.get("expires") != "eot"]
    game.ledger[:] = [e for e in game.ledger if e.get("expires") != "eot"]
    if len(game.seats[game.turn.active].hand) > 7 and game.control.detail.get("action") != "discard":
        game.control = Control("turn_action", [game.turn.active], {"action": "discard"})
        return True
    return False


def _queue_step_triggers(game: Game, step: str) -> None:
    active = game.seats[game.turn.active]
    rad = sum(n for kind, n in active.counters.items() if kind.lower().startswith("rad"))
    if game.mechanics >= 1 and step == "main1" and rad > 0 and active.status == "alive":
        # Rule 728.1: an ability with no source, so no permanent's hook can carry it. In the fourth pod a
        # player remembered it late and put it on the stack by hand.
        game.pending_triggers.append({"id": game.new_id(), "seat": active.index, "source": None,
                                      "text": f"Rad counters (rule 728.1): mill {rad} card(s), one for each of your rad counters; for each nonland "
                                              "card milled this way, lose 1 life and remove one rad counter."})
    due = [e for e in game.ledger if e.get("kind") == "delayed" and e.get("step") == step and e.get("whose", "any") in ("any", game.turn.active)]
    for entry in due:  # once: a delayed trigger is used up by the step it waited for
        game.ledger.remove(entry)
        if game.mechanics >= 3 and entry.get("script") and game.seats[entry["controller"]].status == "alive":
            _stack_script(game, entry, f"the {step} step began")
        elif game.seats[entry["controller"]].status == "alive":
            game.pending_triggers.append({"id": game.new_id(), "seat": entry["controller"], "text": entry.get("text", ""), "source": None})
    for entry in game.ledger:
        if entry.get("kind") == "step_trigger" and entry.get("step") == step \
                and entry.get("whose", "each") in ("each", game.turn.active):
            if game.mechanics >= 3 and entry.get("scripted") and entry.get("script"):
                _stack_script(game, entry, f"the {step} step began")
                continue
            game.pending_triggers.append({"id": game.new_id(), "seat": entry["controller"],
                                          "text": entry.get("text", ""), "source": entry.get("source")})


def collect_triggers(game: Game) -> None:
    """Match what has happened against the ledger's event triggers and queue each match for its
    controller. The table cannot read a card, but it can tell that "a Dragon entered under seat 2"
    fits the pattern the auditor wrote down for Ganax (see `triggers`). A loose match is fine: the
    controller declines a trigger that does not really apply, and the auditor checks."""
    facts, game.facts = game.facts, []
    fired: set[int] = set()  # hooks that have triggered in this batch
    grouped: dict[int, dict] = {}
    for fact in facts:
        if "attachments" not in fact:  # a move records them itself, since it lets go of them
            fact["attachments"] = [o.id for o in game.objects.values() if o.attached_to is not None and o.attached_to == fact.get("id")]
        for hook in game.ledger:
            on = hook.get("on") or {}
            if hook.get("kind") != "event_trigger" or not triggers.matches(on, fact, hook, alternatives_alone=game.mechanics >= 5):
                continue
            if not (on.get("event") or on.get("any_of")):
                continue
            cause = triggers.because(game, fact)
            if game.mechanics >= 3 and hook.get("scripted") and hook.get("script"):
                _stack_script(game, hook, cause, fact)
                continue
            if on.get("once") or game.mechanics >= 2:
                if id(hook) in fired:
                    # The same ability triggering again for something that happened together: one waiting item with a
                    # count, not an item each. Ten attackers made Norin the Wary's controller decline thirty offers.
                    if not on.get("once"):
                        grouped[id(hook)]["count"] += 1
                        grouped[id(hook)]["causes"].append(cause)
                        _describe(grouped[id(hook)], hook)
                    continue
                fired.add(id(hook))
            waiting = {"id": game.new_id(), "seat": hook["controller"], "source": hook.get("source"), "count": 1, "causes": [cause]}
            _describe(waiting, hook)
            grouped[id(hook)] = waiting
            game.pending_triggers.append(waiting)


def _state_based_deaths(game: Game) -> None:
    """Before anyone receives priority (rule 704.3): the deaths the table is sure of. What dies here is seen by
    the triggers collected straight after, as it should be."""
    from . import mechanics  # mechanics reads the model only; imported here to keep this module's imports as they were
    lookup = getattr(game, "_lookup", None)
    if lookup is None:
        return
    for obj, why in mechanics.doomed(game, lookup):
        name = obj.name if obj.public else "A creature"
        gone = mutations.move(game, obj, "graveyard")
        game.announcements.append(f"{name} {why}.")
        if gone is not None and gone.is_commander:
            mutations.move(game, gone, "command")
            game.announcements.append(f"{gone.name} is a commander, and goes from the graveyard to the command zone (rule 903.9a).")


def _stack_script(game: Game, entry: dict, cause: str, fact: dict | None = None) -> None:
    """A trigger whose effect the table carries out itself goes straight on the stack: nobody has to be
    woken to put it there, and nobody to resolve it. It can still be responded to."""
    source = game.objects.get(entry.get("source"))
    it = game.objects.get((fact or {}).get("id"))
    bound = dict(entry.get("bound") or {"self": source.uid if source else None, "it": it.uid if it else None})
    if source is None:  # a delayed trigger belongs to no permanent, but the card it came from is still somewhere ("return Norin the Wary")
        source = next((o for o in game.objects.values() if bound.get("self") is not None and o.uid == bound["self"]), None)
    game.stack.append(StackItem(id=game.new_id(), kind="triggered", controller=entry["controller"], source=entry.get("source"),
                                source_name=source.name if source is not None and source.public else "a triggered ability",
                                text=str(entry.get("text", "")), script=list(entry["script"]), bound=bound))
    game.turn.passed = []
    game.announcements.append(f"The table puts {game.seats[entry['controller']].name}'s trigger on the stack: {entry.get('text', '')} (because {cause})")


def _describe(waiting: dict, hook: dict) -> None:
    causes, count = waiting["causes"], waiting["count"]
    shown = "; ".join(causes[:3]) + (f"; and {count - 3} more" if count > 3 else "")
    waiting["text"] = f"{hook.get('text', '')} (because {shown})" + (f" [triggered {count} times]" if count > 1 else "")


def grant_priority(game: Game, seat: int) -> None:
    """The priority checkpoint: waiting triggers go on the stack, in APNAP order, before anyone
    receives priority. (The audit of what just happened gates this too, once there is an auditor.)"""
    if game.control.kind == "over" or _gate(game, {"do": "grant", "seat": seat}):
        return
    if game.mechanics >= 4:
        _state_based_deaths(game)
    collect_triggers(game)
    owed = {t["seat"] for t in game.pending_triggers}
    for candidate in game.turn_order_from(game.turn.active):
        if candidate in owed:
            game.control = Control("triggers", [candidate], {"then": seat})
            return
    game.pending_triggers.clear()  # any left belong to players who have gone
    _give_priority(game, game.turn_order_from(seat)[0])  # the seat itself, or the next living one in turn order


def _yielding(game: Game, seat: int) -> bool:
    """Whether the seat asked to pass by itself up to a later step of this turn. The request
    lapses as soon as anything is on the stack, the step is reached, or the turn changes."""
    wanted = game.seats[seat].yield_to
    if wanted and wanted["turn"] == game.turn.number and not game.stack and not game.pending_triggers \
            and STEPS.index(game.turn.step) < STEPS.index(wanted["step"]):
        return True
    game.seats[seat].yield_to = None
    return False


def _give_priority(game: Game, seat: int) -> None:
    if _gate(game, {"do": "give", "seat": seat}):
        return
    if game.mechanics >= 4:
        _state_based_deaths(game)  # rule 704.3: whenever a player would receive priority, not only after a resolution
    collect_triggers(game)
    if game.pending_triggers:  # something triggered since priority was last granted: it goes on the stack first
        return grant_priority(game, seat)
    game.control = Control("priority", [seat])
    if _yielding(game, seat):
        pass_priority(game, seat)


def trigger_handled(game: Game, pending_id: int, *, one_of_them: bool = False) -> dict:
    """A waiting trigger has been put on the stack or declined. With `one_of_them`, only one of a
    group that triggered several times is used up; the rest go on waiting."""
    for index, trigger in enumerate(game.pending_triggers):
        if trigger["id"] == pending_id and trigger["seat"] in game.control.seats:
            if one_of_them and trigger.get("count", 1) > 1:
                trigger["count"] -= 1
                trigger["causes"] = trigger["causes"][1:]
                trigger["text"] = trigger["text"].split(" [triggered ")[0] + (f" [triggered {trigger['count']} more times]" if trigger["count"] > 1 else " [once more]")
                return trigger
            del game.pending_triggers[index]
            if not any(t["seat"] == trigger["seat"] for t in game.pending_triggers):
                grant_priority(game, game.control.detail["then"])
            return trigger
    raise TableError("no_such_trigger", "No waiting trigger of yours has that id. Look at the table again.")


def acted(game: Game) -> None:
    """The priority holder did something: everyone gets to respond again, and they keep priority."""
    game.turn.passed = []


def pass_priority(game: Game, seat: int) -> None:
    turn = game.turn
    turn.passed.append(seat)
    living = game.turn_order_from(seat + 1)
    waiting = [s for s in living if s not in turn.passed]
    if waiting:
        _give_priority(game, waiting[0])
    elif game.stack:
        _begin_resolution(game)
    else:
        advance(game)


def _begin_resolution(game: Game) -> None:
    item: StackItem = game.stack.pop()
    game.turn.passed = []
    if item.auto_permanent and item.obj in game.objects:
        mutations.move(game, game.objects[item.obj], "battlefield", controller=item.controller)
        return grant_priority(game, game.turn.active)
    if item.script:
        from . import ops, scripts  # ops imports this module
        told = ops.Told()
        scripts.run(game, item.script, item.controller, item.bound, lambda op: ops.apply_op(game, None, item.controller, op, told))
        game.announcements.append(f"{item.source_name} ({game.seats[item.controller].name}) resolves by script: "
                                  + (" ".join(told.public) or "it does nothing now."))
        return grant_priority(game, game.turn.active)
    game.resolution = {"item": item, "batches": 0, "status": "open"}
    game.control = Control("resolution", [item.controller])


def finish_resolution(game: Game, card_to: str = "graveyard", enters: dict | None = None) -> None:
    item: StackItem = game.resolution["item"]
    obj = game.objects.get(item.obj) if item.obj is not None else None
    if obj is not None and obj.zone == "stack":
        mutations.move(game, obj, card_to, controller=item.controller, enters=enters)
    game.resolution = None
    game.requests[:] = [r for r in game.requests if r["status"] == "pending"]
    grant_priority(game, game.turn.active)


def request_choice(game: Game, requester: int, seats: list[int], prompt: str, options: list | None) -> dict:
    order = [s for s in game.turn_order_from(game.turn.active) if s in seats]
    if not order:
        raise TableError("bad_request", "Name at least one living seat to ask.")
    request = {"id": game.new_id(), "from": requester, "prompt": prompt, "options": options,
               "waiting": order, "answers": {}, "status": "pending",
               "return_to": {"kind": game.control.kind, "seats": list(game.control.seats),
                             "detail": dict(game.control.detail)}}
    game.requests.append(request)
    if game.resolution:
        game.resolution["status"] = "suspended"
    game.control = Control("request", [order[0]], {"request": request["id"]})
    return request


def answer(game: Game, seat: int, request_id: int, value) -> None:
    request = next((r for r in game.requests if r["id"] == request_id and r["status"] == "pending"), None)
    if request is None or not request["waiting"] or request["waiting"][0] != seat:
        raise TableError("no_such_request", "No question is waiting for your answer with that id.")
    if request["options"] and value not in request["options"]:
        raise TableError("bad_answer", f"Answer with one of: {request['options']}.")
    request["answers"][str(seat)] = value
    request["waiting"].pop(0)
    request["waiting"][:] = [s for s in request["waiting"] if game.seats[s].status == "alive"]
    if request["waiting"]:
        game.control = Control("request", [request["waiting"][0]], {"request": request_id})
        return
    request["status"] = "answered"
    if game.resolution:
        game.resolution["status"] = "open"
    game.control = Control(**request.pop("return_to"))


def turn_action_done(game: Game) -> None:
    """A turn-based action is complete; move on to whoever acts next."""
    detail, turn = game.control.detail, game.turn
    action = detail["action"]
    if action == "declare_blockers" and detail.get("waiting"):
        game.control = Control("turn_action", detail["waiting"][:1],
                               {"action": "declare_blockers", "waiting": detail["waiting"][1:]})
    elif action == "discard":
        game.control = Control("turn_action", [], {"action": "discard"})  # marks the discard as done
        if not _cleanup(game):
            advance(game)
    else:
        if action == "declare_attackers" and not turn.attackers:
            turn.remaining[:] = [s for s in turn.remaining if s not in ("declare_blockers", "combat_damage")]
        grant_priority(game, turn.active)


def held_step_done(game: Game) -> None:
    step = game.control.detail["step"]
    if step in _PRIORITY_STEPS:
        _queue_step_triggers(game, step)
        return grant_priority(game, game.turn.active)
    if step == "cleanup" and _cleanup(game):
        return
    advance(game)


def repair_after_elimination(game: Game) -> None:
    """Someone left while holding the pen, or during their own turn."""
    if game.control.kind == "over":
        return
    turn, control = game.turn, game.control
    alive = set(game.alive())
    for request in game.requests:
        request["waiting"][:] = [s for s in request["waiting"] if s in alive]
    if game.resolution and game.resolution["item"].controller not in alive:
        game.resolution = None
        return grant_priority(game, turn.active)
    if control.seats and not set(control.seats) & alive:
        if control.kind == "priority":
            return pass_priority(game, control.seats[0])
        if control.kind == "request":
            request = next(r for r in game.requests if r["id"] == control.detail["request"])
            if request["waiting"]:
                game.control = Control("request", [request["waiting"][0]], control.detail)
            else:
                request["status"] = "answered"
                game.control = Control(**request.pop("return_to"))
            return
        if control.kind == "turn_action":
            return turn_action_done(game)
        grant_priority(game, turn.active)
