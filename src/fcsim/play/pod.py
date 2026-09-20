"""A multiplayer game: one agent per seat, an auditor behind them, a directory of what happened.

The scheduler is simple on purpose. It wakes whichever seat the table is waiting for, passes
for a seat that has priority on someone else's turn with nothing on the stack, audits at every
checkpoint where something auditable happened, and turns the auditor's findings into waiting
triggers and correction wakes. Narrower watches, triage and disputes come later (PLAN.md §6, §8).
"""

from __future__ import annotations

import secrets
from pathlib import Path

from ..agents import BudgetExceeded, Runner, RunnerError, cost_summary
from concurrent.futures import ThreadPoolExecutor

from ..audit import SERIOUS, Auditor, describe
from ..audit.auditor import card_reference
from ..cards import CardDatabase, read_decklist, resolve, validate_deck
from ..mcp import McpServer
from ..rules import RulesReference
from ..table import Table
from ..tools import SeatTools
from .gamedir import GameDirectory
from .goldfish import _deck_reference

SYSTEM = """\
You are {name}, one of {count} players in a game of Magic: The Gathering (Commander) at a virtual \
table, played through tools. Play to win, as a strong and fair player would: develop your board, \
judge who is the real threat, hold interaction for what matters, attack where it helps you most. \
Talk to the table with `say` when it serves you: make deals, point at threats, bluff about what \
you hold. Deals are not binding. Never misstate the public game state or the rules.

How the table works. The table is a tabletop, not a rules engine: it knows zones, turns and \
priority, and no card text. You carry out every rule and card effect yourself with your tools, \
honestly and exactly, and a rules auditor checks each stretch of play. If it finds an error of \
yours you will be asked to correct it. Read a card with lookup_card before relying on what it \
does, including opponents' cards, and look a rule up when you are unsure.
- get_view shows everything you may see. `pen` says who the table is waiting for and for what. \
Cards are named by handles, which change whenever a card changes zones: look again after acting.
- When you cast a spell you keep priority; pass priority to let it resolve. Unless you cast it \
as "auto_permanent", the table later waits for you to resolve it: use resolve, as many times as \
the effect needs, then finish_resolution.
- Pay costs by naming the permanents you tap in `payment`. Tap only what the cost needs.
- On your own turn, do not pass one step at a time: use pass_priority with `until`, and when you \
are finished use until "cleanup", which passes through the rest of your turn. The table passes \
for you through your own upkeep and draw steps when nothing is waiting there.
- Put your triggered abilities on the stack (add_trigger) and carry out state-based actions.

You are woken only when the table needs you: on your own turn, when something is on the stack \
that you may respond to, to block, for a trigger or a question, or to correct an error. If you \
are woken to respond and have no response, just pass priority. Your private notes are your only \
memory between wakes. Every wake begins with a briefing: the table as it stands, with the handles \
you need; your hand and the cards in play, with their text; and your notes. Act on it directly. \
Call get_view only after you have changed the table and need fresh handles, and lookup_card only \
for a card the briefing does not show. Update your notes with write_notes when your plans or your \
read of the table change. When you pass priority and nobody responds, the same wake carries on: \
the result says `carry_on` and shows the table again. A wake ends when the table stops waiting \
for you: the result then says `wake`. Then stop calling \
tools and reply with one or two sentences on what you did and why; only you will see them."""

_TRIAGE = {"type": "object", "additionalProperties": False, "required": ["respond", "why"],
           "properties": {"respond": {"type": "boolean"}, "why": {"type": "string", "description": "One short sentence."}}}
_DEFAULTS = {"priority": "pass_priority", "mulligan": "keep", "declare_attackers": "declare_attackers",
             "declare_blockers": "declare_blockers", "combat_damage": "combat_damage", "discard": "finish_step",
             "held_step": "finish_step", "resolution": "finish_resolution", "correction": "finish_step"}
_WHY = {"mulligan": "Your opening hand has been dealt. Keep it or mulligan.",
        "priority": "You have priority.", "resolution": "Your spell or ability is resolving: carry out its effect.",
        "triggers": "One or more of your triggered abilities are waiting to go on the stack.",
        "request": "Another player's effect asks you to choose.", "declare_attackers": "Declare your attackers.",
        "declare_blockers": "You are being attacked: declare blockers.", "combat_damage": "Deal this step's combat damage.",
        "discard": "It is your cleanup step and you have more than seven cards: discard down, then finish_step.",
        "held_step": "The table has handed you a step it must not perform by itself."}


def _names(decks: list) -> list[str]:
    names = []
    for index, deck in enumerate(decks):
        name = deck.commanders[0].name.split(",")[0].split(" the ")[0]
        names.append(name if name not in names else f"{name} {index}")
    return names


def play_pod(deck_paths: list[str | Path], runners: list[Runner], auditor_runner: Runner, database: CardDatabase, *, cycles: int = 3,
             seed: int | None = None, out_root: str | Path = "games", rules: RulesReference | None = None,
             resume: str | Path | None = None, audit_every: str = "turn", triage_runners: list[Runner] | None = None,
             echo=lambda line: print(line, flush=True)) -> dict:
    """Play up to `cycles` turns per seat. `runners[i]` pilots seat i; share one Budget across all
    runners and the auditor to cap the whole game.

    With `resume`, play `cycles` more rounds of the game in that directory: its journal is replayed
    into a table, the decks are the copies it kept, and players pick up from their notes.
    `audit_every` is "turn" (one audit of each whole turn, before the next begins) or "checkpoint"
    (an audit every time the pen moves after an action: stricter and several times slower).
    `triage_runners[i]` answers seat i's respond-or-pass questions, if given: a cheaper or
    lower-effort runner than the one that plays the seat."""
    earlier = GameDirectory.open(resume) if resume else None
    if earlier:
        deck_paths = [earlier.path / "decks" / f"seat-{index}.txt" for index in range(len(earlier.read("config.json")["seats"]))]
    decks = [resolve(read_decklist(path), database, online=True) for path in deck_paths]
    for path, deck in zip(deck_paths, decks, strict=True):
        if deck.unknown or not deck.commanders:
            raise ValueError(f"{path} has unrecognized cards or no commander; run `fcsim deck check` on it first.")
    names, count = _names(decks), len(decks)
    if earlier:
        return _play(earlier, Table.replay(earlier.read("config.json")["table"], earlier.events(),
                                           card_lookup=lambda n: (c := database.find(n)) and {"mana_value": c.mana_value}),
                     earlier.read("config.json")["seats"], decks, runners, auditor_runner, database, cycles, rules, echo,
                     earlier=earlier.read("report.json") if (earlier.path / "report.json").is_file() else {"wakes": []},
                     audit_every=audit_every, triage_runners=triage_runners)
    seed = seed if seed is not None else secrets.randbelow(2 ** 31)
    directory = GameDirectory(out_root, "pod_" + "-".join(names), seed)
    table = Table([{"name": name, "commanders": [c.name for c in deck.commanders], "library": [c.name for c in deck.library()]}
                   for name, deck in zip(names, decks, strict=True)], seed=seed, gated=True, on_event=directory.journal,
                  card_lookup=lambda n: (c := database.find(n)) and {"mana_value": c.mana_value})
    for index, path in enumerate(deck_paths):
        directory.write(f"decks/seat-{index}.txt", read_decklist(path).to_mtgo())
    directory.pin_cards({card.name for deck in decks for card in [*deck.commanders, *(c for c, _ in deck.cards)]}, database)
    directory.pin_rules(rules)
    directory.write("config.json", {"mode": "pod", "decks": [str(p) for p in deck_paths], "seats": names, "cycles": cycles,
                                    "table": table.config, "models": [r.model for r in runners],
                                    "backends": [getattr(r, "backend", "custom") for r in runners],
                                    "auditor_model": auditor_runner.model,
                                    "auditor_backend": getattr(auditor_runner, "backend", "custom"),
                                    "deck_issues": [[i.to_dict() for i in validate_deck(d, database)] for d in decks]})
    return _play(directory, table, names, decks, runners, auditor_runner, database, cycles, rules, echo,
                 audit_every=audit_every, triage_runners=triage_runners)


def _play(directory: GameDirectory, table: Table, names: list[str], decks: list, runners: list, auditor_runner, database: CardDatabase,
          cycles: int, rules: RulesReference | None, echo, *, earlier: dict | None = None, audit_every: str = "turn",
          triage_runners: list | None = None) -> dict:
    """Schedule seats and audits until the game ends or `cycles` more rounds have been played."""
    count, seed = len(names), table.config["seed"]
    table.on_event = directory.journal  # a replayed table was built without its journal
    # A game's length is counted in rounds, each living player's own turns, so that eliminations do
    # not shorten it. On a resume the turn in progress (or just begun) is the first of the new rounds.
    game = table.game
    last_round = (game.seats[game.turn.active].turns_taken - 1 if earlier is not None else 0) + cycles
    table.card_lookup = lambda n: (c := database.find(n)) and {"mana_value": c.mana_value, "types": sorted(c.types)}
    auditor = Auditor(auditor_runner, database, context=f"A {count}-player Commander game. Seats, in turn order: "
                      + ", ".join(f"{name} (seat {i})" for i, name in enumerate(names)) + ".")
    systems = [SYSTEM.format(name=name, count=count) + "\n\nYOUR DECK (count, name, cost, type, text)\n"
               + _deck_reference(deck, database) for name, deck in zip(names, decks, strict=True)]
    current: dict = {}
    # Each seat's place in the log, so a wake can say what happened since its last one. After a
    # resume the players start from their notes and the board, not from the whole history again.
    seen = [len(table.events) if earlier is not None else 0] * count
    decisions: dict = {}  # triage answers for the stack as it currently stands
    report: dict = {"directory": str(directory.path), "seed": seed, "seats": names, "stopped": None,
                    "triage": list((earlier or {}).get("triage", [])),
                    "wakes": list((earlier or {}).get("wakes", [])), "audits": list((earlier or {}).get("audits", [])),
                    "earlier_usage": [*(earlier or {}).get("earlier_usage", []), *([earlier["usage"]] if (earlier or {}).get("usage") else [])]}

    def holds_pen(seat: int) -> bool:
        return not table.awaiting_audit and seat in table.game.control.seats

    def tools_for(token: str):
        tools = current.get("tools")
        if tools is None or token != current.get("token"):
            return None

        def call(request_id: str, name: str, arguments: dict) -> dict:
            result = tools.call(request_id, name, arguments)
            me = current["seat"]
            if not holds_pen(me) and not current["ended"]:
                # The pen has left this seat. A new session costs seconds of start-up and a fresh read of
                # everything, so first do what needs no other player's full attention: if nobody responds
                # and the pen comes straight back, this wake simply carries on.
                try:
                    back = settle(me) == "yours"
                except RunnerError:
                    back = False  # a failed respond-or-pass question; the main loop will meet it again and report it
                if back:
                    control = table.game.control
                    why = control.detail.get("action", control.kind)
                    result = {**result, "pen": {"kind": why, "seats": control.seats},
                              "carry_on": f"Nobody else acted and the table is waiting for you again: {_WHY.get(why, why)} "
                                          "Here is the table as it now stands.", "news": news(me), "table": briefing(me)}
                    seen[me] = len(table.events)
                else:
                    table.close_wake(current["wake"])
                    current["ended"] = True
            if isinstance(result, dict) and "pen" in result and current["ended"]:
                result["wake"] = "The table is no longer waiting for you. Stop calling tools and give your summary."
            return result
        return tools.definitions, call

    def when() -> str:
        game = table.game
        return (f"Round {game.seats[game.turn.active].turns_taken}, {names[game.turn.active]}'s turn "
                f"(table turn {game.turn.number}), {game.turn.step} step.")

    def briefing(seat: int) -> str:
        """Everything a seat would otherwise spend two or three round trips fetching."""
        view = table.view(seat)
        mine = view["seats"][seat]["hand"].get("cards", [])
        in_play = [o.get("name") for o in view["battlefield"]] + [i["source"] for i in view["stack"]] \
            + [o.get("name") for s in view["seats"] for zone in ("command", "graveyard", "exile") for o in s[zone]]
        hand = []
        for card in mine:
            known = database.find(card["name"])
            face = known.front if known else None
            stats = f" {face.power}/{face.toughness}" if face and face.power is not None else ""
            hand.append(f"- [{card['handle']}] {card['name']}" + (f" {known.mana_cost} [{face.type_line}{stats}] "
                                                                   f"{' '.join(known.text.split())}" if known else ""))
        waiting = [f"- [{t['handle']}] {t['text']}" for t in view["waiting_triggers"] if t["seat"] == seat]
        asked = [f"- [{q['handle']}] from {names[q['from']]}: {q['prompt']}" + (f" Options: {q['options']}" if q["options"] else "")
                 for q in view["questions"] if q["waiting"] and q["waiting"][0] == seat]
        # A wake is a fresh session: without this a player repeats what it did a moment ago, such as
        # putting the same end-step trigger on the stack again each time it gets priority in that step.
        done = [f"- [{e['step']}] " + " ".join(e["public"]) for e in table.events
                if e["seat"] == seat and e["turn"] == table.game.turn.number and e["public"]
                and e["tool"] not in ("pass_priority", "write_notes")][-25:]
        return "\n".join(["WHAT YOU HAVE ALREADY DONE THIS TURN (do not repeat it: each ability triggers once per event)",
                          *(done or ["(nothing yet)"]), "",
                          "THE TABLE (the codes in brackets are your handles; NOW is a creature's current power and toughness as "
                          "the auditor last worked it out: trust it unless the board has changed since)",
                          describe(view, database), "", "YOUR HAND", *(hand or ["(empty)"]),
                          *(["", "YOUR TRIGGERS WAITING TO GO ON THE STACK", *waiting] if waiting else []),
                          *(["", "QUESTIONS FOR YOU", *asked] if asked else []),
                          "", "CARDS IN PLAY", card_reference([n for n in in_play if n], database, rulings_per_card=0) or "(none)",
                          "", "YOUR NOTES", table.notes[seat] or "(none yet)"])

    def news(seat: int) -> str:
        events = table.act(seat, "get_log", {"since": seen[seat]})["events"]
        lines = [f"[{e['step']}] " + " ".join(e["text"]) + (" (to you: " + " ".join(e["private"]) + ")" if e.get("private") else "")
                 for e in events if (e["text"] or e.get("private")) and e["tool"] != "pass_priority"][-40:]  # passes are noise
        return "Since your last wake:\n" + "\n".join(lines) if lines else "Nothing has happened since your last wake."

    def triage(seats: list[int]) -> dict[int, bool]:
        """Ask several seats at once, each with one question and no tools, whether they want to
        respond to what is on the stack. Most answers are no, and a no costs seconds, not a wake."""
        game = table.game
        top = game.stack[-1]
        question = (f"{names[top.controller]}'s {top.source_name} is on top of the stack and you have priority. Do you want to "
                    "respond before it resolves, by casting a spell or activating an ability? Answer respond=false to pass. "
                    "Answer respond=true only if you would actually act now; you will then be woken to do it.")
        prompts = {seat: f"{when()} {question}\n\n"
                         f"{news(seat)}\n\n{briefing(seat)}" for seat in seats}

        def one(seat: int):
            return seat, (triage_runners or runners)[seat].ask(systems[seat], prompts[seat], schema=_TRIAGE)
        with ThreadPoolExecutor(max_workers=len(seats)) as pool:
            replies = list(pool.map(one, seats))
        answers = {}
        for seat, reply in replies:
            answers[seat] = bool((reply.data or {}).get("respond"))
            record = {"turn": game.turn.number, "seat": seat, "respond": answers[seat], "why": (reply.data or {}).get("why", ""),
                      "about": top.source_name, "seconds": round(reply.seconds, 1)}
            report["triage"].append(record)
            directory.append(f"transcript-seat-{seat}.jsonl", {"kind": "triage", **record})
        echo(f"turn {game.turn.number} {game.turn.step}: response to {top.source_name}: "
             + ", ".join(f"{names[s]} {'RESPONDS' if a else 'passes'}" for s, a in answers.items()))
        return answers

    def wake(server: McpServer, seat: int, kind: str, extra: str = "") -> None:
        token, handle = secrets.token_hex(16), table.open_wake(seat, kind)
        current.update(tools=SeatTools(table, seat, handle, database=database, rules=rules), token=token, wake=handle,
                       seat=seat, ended=False)
        game = table.game
        prompt = f"{when()} {extra or _WHY.get(kind, 'The table is waiting for you.')}\n\n{news(seat)}\n\n{briefing(seat)}"
        echo(f"turn {game.turn.number} {game.turn.step}: {names[seat]} ({kind}) ...")
        started = len(table.events)
        reply = runners[seat].ask(systems[seat], prompt, tools=server.connection(
            token, tuple(t["name"] for t in current["tools"].definitions)))
        table.close_wake(handle)
        forced = None
        if holds_pen(seat) and table.game.control.kind != "setup" or (kind == "mulligan" and holds_pen(seat)):
            forced = _default_action(table, seat)  # it stopped while the table still waited for it
        seen[seat] = len(table.events)
        entry = {"seat": seat, "turn": game.turn.number, "kind": kind, "summary": reply.text, "events": [started, len(table.events)],
                 "tool_calls": len(current["tools"].calls), "tool_errors": [c for c in current["tools"].calls if c["error"]],
                 "forced": forced, "seconds": round(reply.seconds, 1), "estimated_cost_usd": reply.estimated_cost_usd}
        report["wakes"].append(entry)
        directory.append(f"transcript-seat-{seat}.jsonl", {**entry, "calls": current["tools"].calls})
        directory.write(f"notes-seat-{seat}.md", table.notes[seat])
        current.update(tools=None, token=None, wake=None)

    def audit(server: McpServer, snapshot: tuple) -> None:
        unit = table.events[snapshot[2]:]
        if not any(e["seat"] is not None and e["public"] and e["tool"] not in ("pass_priority", "say", "write_notes", "keep", "mulligan")
                   for e in unit):
            return
        echo(f"         auditing {len(unit)} events ...")
        verdict, reply = auditor.audit(table, snapshot)
        record = {"turn": table.game.turn.number, "events": [snapshot[2], len(table.events)], **verdict, "seconds": round(reply.seconds, 1)}
        report["audits"].append(record)
        directory.append("audits.jsonl", record)
        if verdict.get("hooks"):
            try:
                table.apply_hooks(verdict["hooks"])
            except ValueError as error:
                record["hooks_error"] = str(error)
        table.apply_computed(verdict.get("computed") or [])
        actors = [e["seat"] for e in unit if e["seat"] is not None]
        at_fault: dict[int, list[str]] = {}
        for finding in verdict.get("findings", []):
            seat = finding.get("seat")
            seat = seat if isinstance(seat, int) and 0 <= seat < count and table.game.seats[seat].status == "alive" else None
            if finding["kind"] == "trigger" and seat is not None:
                table.queue_trigger(seat, finding["summary"])
            elif finding["kind"] in SERIOUS and finding["kind"] != "trigger":
                seat = seat if seat is not None else (actors[-1] if actors else None)
                if seat is not None:
                    at_fault.setdefault(seat, []).append(f"{finding['summary']} Basis: {finding['basis']} What should happen: {finding['fix']}")
        for seat, problems in at_fault.items():
            table.open_correction(seat, " | ".join(problems))
            wake(server, seat, "correction", "The rules auditor found a problem with what you did. Put the table right with `act` "
                 "(say what you are correcting in `because`), then call finish_step. If you are sure the auditor is wrong, "
                 "explain why with `say` and call finish_step without changing anything.\n" + "\n".join(f"- {p}" for p in problems))
            if table.game.control.kind == "correction":
                table.act(seat, "finish_step")

    def settle(me: int | None = None) -> str:
        """Do everything that needs no full wake: release mid-turn checkpoints, pass for seats with
        nothing to respond to, ask the rest whether they respond. Returns why it stopped: "over",
        "stuck", "audit" (the table waits for one), "wake" (some seat must decide), or "yours" when
        called from inside seat `me`'s wake and the pen has come back to it."""
        while True:
            game = table.game
            if game.control.kind == "over" or game.seats[game.turn.active].turns_taken > last_round:
                return "over"
            if table.awaiting_audit:
                # By default a turn is audited whole, before the next one begins: that is when its
                # hooks must exist. Checkpoints inside the turn are released at once.
                if audit_every == "checkpoint" or game.control.detail["resume"]["do"] == "next_turn":
                    return "audit"
                table.release()
                continue
            expected = table.expected()
            if not expected:
                return "stuck"
            seat, kind = expected[0]
            if seat == me:
                return "yours"
            if kind == "priority" and not game.stack and (seat != game.turn.active or game.turn.step in ("upkeep", "draw")):
                # Nothing to respond to on someone else's turn; and nothing waiting in one's own
                # upkeep or draw step, or the table would be asking for a trigger, not priority.
                table.act(seat, "pass_priority")
                continue
            if kind in ("priority", "declare_blockers") and _cannot_act(table, seat, kind):
                table.act(seat, _DEFAULTS[kind])  # an empty hand and nothing untapped; or nothing that could block
                continue
            if kind == "priority" and game.stack:
                # A chance to respond. Ask everyone still to act at once, and remember the answers
                # for as long as the stack stays as it is.
                signature = tuple(item.id for item in game.stack)
                if decisions.get("stack") != signature:
                    waiting = [s for s in game.turn_order_from(seat) if s not in game.turn.passed and s != me]
                    decisions.clear()
                    decisions.update(stack=signature, **{str(s): a for s, a in triage(waiting).items()})
                if not decisions.get(str(seat), True):
                    table.act(seat, "pass_priority")
                    seen[seat] = len(table.events)
                    continue
            return "wake"

    with McpServer(tools_for) as server:
        try:
            snapshot = auditor.snapshot(table)
            while True:
                why = settle()
                if why in ("over", "stuck"):
                    break
                if why == "audit":
                    audit(server, snapshot)
                    table.release()
                    snapshot = auditor.snapshot(table)
                    directory.write("state.json", table.game.to_dict())
                    continue
                seat, kind = table.expected()[0]
                before = len(table.events)
                wake(server, seat, kind)
                if len(table.events) == before and table.expected() and table.expected()[0] == (seat, kind):
                    report["stopped"] = f"{names[seat]} could not act ({kind})"  # never spin
                    break
        except BudgetExceeded as stop:
            report["stopped"] = str(stop)
        except RunnerError as error:
            report["stopped"] = f"model error: {error}"
    game = table.game
    report["standings"] = [{"seat": s.index, "name": s.name, "status": s.status, "life": s.life, "hand": len(s.hand),
                            "permanents": sum(1 for i in game.battlefield if game.objects[i].controller == s.index),
                            "commander_damage": s.commander_damage, "commander_casts": s.commander_casts} for s in game.seats]
    report["winner"] = names[game.winner] if game.winner is not None else None
    report["turns_played"] = game.turn.number
    report["rounds"] = max(s.turns_taken for s in game.seats)
    report["usage"] = auditor_runner.budget.to_dict()
    directory.write("state.json", game.to_dict())
    directory.write("usage.json", report["usage"])
    directory.write("report.json", report)
    directory.write("report.md", render(report))
    return report


def _cannot_act(table: Table, seat: int, kind: str) -> bool:
    """True only when it is certain: for a response, no cards in hand and nothing untapped; for
    blocks, no untapped permanent that is, or might be, a creature."""
    game = table.game
    mine = [game.objects[i] for i in game.battlefield if game.objects[i].controller == seat and not game.objects[i].tapped]
    if kind == "priority":
        return bool(game.stack) and not game.seats[seat].hand and not mine

    def maybe_creature(obj) -> bool:
        info = table.card_lookup(obj.name) if table.card_lookup and obj.token is None and not obj.face_down else None
        return info is None or "Creature" in info.get("types", []) or bool(obj.annotations)
    return not any(maybe_creature(obj) for obj in mine)


def _default_action(table: Table, seat: int) -> str | None:
    """What the driver does for a seat that stopped while the table still waited for it."""
    control = table.game.control
    kind = control.detail.get("action", control.kind)
    kind = "mulligan" if kind == "setup" else kind
    view = table.view(seat)
    if kind == "triggers":
        mine = [t for t in view["waiting_triggers"] if t["seat"] == seat]
        result = table.act(seat, "skip_trigger", {"pending": mine[0]["handle"], "because": "the player did not act"}) if mine else {}
    elif kind == "request":
        question = next((q for q in view["questions"] if q["waiting"] and q["waiting"][0] == seat), None)
        result = table.act(seat, "answer", {"request": question["handle"], "answer": (question["options"] or ["no"])[0]}) if question else {}
    else:
        result = table.act(seat, _DEFAULTS.get(kind, "finish_step"))
    return None if "error" in result else f"the driver chose `{kind}` by default"


def render(report: dict) -> str:
    lines = [f"# Pod: {', '.join(report['seats'])}", "",
             f"Seed {report['seed']}. {report.get('rounds', '?')} rounds ({report['turns_played']} table turns). "
             + (f"Winner: {report['winner']}." if report["winner"] else "No winner yet."),
             "", "| Seat | Status | Life | Hand | Permanents | Commander casts | Commander damage taken |", "| --- | --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {s['name']} | {s['status']} | {s['life']} | {s['hand']} | {s['permanents']} | "
              f"{', '.join(f'{k}: {v}' for k, v in s['commander_casts'].items()) or '-'} | "
              f"{', '.join(f'{k}: {v}' for k, v in s['commander_damage'].items()) or '-'} |" for s in report["standings"]]
    lines += ["", "## What each player said they were doing", ""]
    for entry in report["wakes"]:
        summary = " ".join(entry["summary"].split())
        if summary and entry["kind"] != "mulligan":
            lines.append(f"- Turn {entry['turn']}, **{report['seats'][entry['seat']]}** ({entry['kind']}): {summary}"
                         + (f" _({entry['forced']})_" if entry["forced"] else ""))
    serious = [(a["turn"], f) for a in report["audits"] for f in a.get("findings", []) if f["kind"] in SERIOUS]
    lines += ["", "## Rules health", "",
              f"{len(report['audits'])} audits; {len(serious)} finding(s); "
              f"{sum(len(w['tool_errors']) for w in report['wakes'])} rejected tool call(s); "
              f"{sum(1 for w in report['wakes'] if w['forced'])} wake(s) the driver had to finish."]
    lines += [f"- Turn {turn}, {f['kind']}: {f['summary']}" for turn, f in serious]
    usage = report["usage"]
    lines += ["", "## Usage", "", f"{usage['calls']} model calls, {usage['seconds']:.0f} seconds of model time, "
              f"{cost_summary(usage)}.",
              "", "`log.md` has the play-by-play, table talk included."]
    if report["stopped"]:
        lines += ["", f"**Stopped early:** {report['stopped']}"]
    return "\n".join(lines) + "\n"
