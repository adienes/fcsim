"""A multiplayer game: one agent per seat, an auditor behind them, a directory of what happened.

The scheduler is simple on purpose. It wakes whichever seat the table is waiting for, passes
for a seat that has priority on someone else's turn with nothing on the stack, audits at every
checkpoint where something auditable happened, and turns the auditor's findings into waiting
triggers and correction wakes. Narrower watches, triage and disputes come later (PLAN.md §6, §8).
"""

from __future__ import annotations

import secrets
from pathlib import Path

from ..agents import BudgetExceeded, RunnerError
from ..audit import SERIOUS, Auditor
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
- On your own turn, do not pass one step at a time: use pass_priority with `until`.
- Put your triggered abilities on the stack (add_trigger) and carry out state-based actions.

You are woken only when the table needs you: on your own turn, when something is on the stack \
that you may respond to, to block, for a trigger or a question, or to correct an error. If you \
are woken to respond and have no response, just pass priority. Your private notes are your only \
memory between wakes: read them first, and update them with write_notes when your plans or your \
read of the table change. A wake ends when the table stops waiting for you. Then stop calling \
tools and reply with one or two sentences on what you did and why; only you will see them."""

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


def play_pod(deck_paths: list[str | Path], runners: list, auditor_runner, database: CardDatabase, *, cycles: int = 3,
             seed: int | None = None, out_root: str | Path = "games", rules: RulesReference | None = None,
             echo=lambda line: print(line, flush=True)) -> dict:
    """Play up to `cycles` turns per seat. `runners[i]` pilots seat i; share one Budget across all
    runners and the auditor to cap the whole game."""
    decks = [resolve(read_decklist(path), database, online=True) for path in deck_paths]
    for path, deck in zip(deck_paths, decks, strict=True):
        if deck.unknown or not deck.commanders:
            raise ValueError(f"{path} has unrecognized cards or no commander; run `fcsim deck check` on it first.")
    names, count = _names(decks), len(decks)
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
                                    "auditor_model": auditor_runner.model,
                                    "deck_issues": [[i.to_dict() for i in validate_deck(d, database)] for d in decks]})
    auditor = Auditor(auditor_runner, database, context=f"A {count}-player Commander game. Seats, in turn order: "
                      + ", ".join(f"{name} (seat {i})" for i, name in enumerate(names)) + ".")
    systems = [SYSTEM.format(name=name, count=count) + "\n\nYOUR DECK (count, name, cost, type, text)\n"
               + _deck_reference(deck, database) for name, deck in zip(names, decks, strict=True)]
    current: dict = {}
    seen = [0] * count          # each seat's place in the log, so a wake can say what happened since its last one
    report: dict = {"directory": str(directory.path), "seed": seed, "seats": names, "wakes": [], "audits": [], "stopped": None}

    def holds_pen(seat: int) -> bool:
        return not table.awaiting_audit and seat in table.game.control.seats

    def tools_for(token: str):
        tools = current.get("tools")
        if tools is None or token != current.get("token"):
            return None

        def call(request_id: str, name: str, arguments: dict) -> dict:
            result = tools.call(request_id, name, arguments)
            if not holds_pen(current["seat"]):  # the pen has moved on, or the table has stopped for an audit
                table.close_wake(current["wake"])
                current["ended"] = True
            if isinstance(result, dict) and "pen" in result and current["ended"]:
                result["wake"] = "The table is no longer waiting for you. Stop calling tools and give your summary."
            return result
        return tools.definitions, call

    def wake(server: McpServer, seat: int, kind: str, extra: str = "") -> None:
        token, handle = secrets.token_hex(16), table.open_wake(seat, kind)
        current.update(tools=SeatTools(table, seat, handle, database=database, rules=rules), token=token, wake=handle,
                       seat=seat, ended=False)
        news = table.act(seat, "get_log", {"since": seen[seat]})["events"]
        lines = [f"[{e['step']}] " + " ".join(e["text"]) + (" (to you: " + " ".join(e["private"]) + ")" if e.get("private") else "")
                 for e in news if e["text"] or e.get("private")][-40:]
        game = table.game
        prompt = (f"Table turn {game.turn.number}, {names[game.turn.active]}'s turn, {game.turn.step} step. "
                  f"{extra or _WHY.get(kind, 'The table is waiting for you.')}\n\n"
                  + ("Since your last wake:\n" + "\n".join(lines) if lines else "Nothing has happened since your last wake."))
        echo(f"turn {game.turn.number} {game.turn.step}: {names[seat]} ({kind}) ...")
        started = len(table.events)
        reply = runners[seat].ask(systems[seat], prompt, mcp_config=server.config(token),
                                  allowed_tools=tuple(f"mcp__{server.name}__{t['name']}" for t in current["tools"].definitions))
        table.close_wake(handle)
        forced = None
        if holds_pen(seat) and table.game.control.kind != "setup" or (kind == "mulligan" and holds_pen(seat)):
            forced = _default_action(table, seat)  # it stopped while the table still waited for it
        seen[seat] = len(table.events)
        entry = {"seat": seat, "turn": game.turn.number, "kind": kind, "summary": reply.text, "events": [started, len(table.events)],
                 "tool_calls": len(current["tools"].calls), "tool_errors": [c for c in current["tools"].calls if c["error"]],
                 "forced": forced, "seconds": round(reply.seconds, 1), "estimated_cost_usd": round(reply.cost_usd, 4)}
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

    with McpServer(tools_for) as server:
        try:
            snapshot = auditor.snapshot(table)
            while table.game.control.kind != "over" and table.game.turn.number <= cycles * count:
                if table.awaiting_audit:
                    audit(server, snapshot)
                    table.release()
                    snapshot = auditor.snapshot(table)
                    directory.write("state.json", table.game.to_dict())
                    continue
                expected = table.expected()
                if not expected:
                    break
                seat, kind = expected[0]
                if kind == "priority" and seat != table.game.turn.active and not table.game.stack:
                    table.act(seat, "pass_priority")  # nothing to respond to on someone else's turn
                    continue
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
    report["usage"] = auditor_runner.budget.to_dict()
    directory.write("state.json", game.to_dict())
    directory.write("usage.json", report["usage"])
    directory.write("report.json", report)
    directory.write("report.md", render(report))
    return report


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
             f"Seed {report['seed']}. {report['turns_played']} table turns. "
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
              f"${usage['estimated_cost_usd']:.2f} estimated at list prices (plan usage on a subscription, not a charge).",
              "", "`log.md` has the play-by-play, table talk included."]
    if report["stopped"]:
        lines += ["", f"**Stopped early:** {report['stopped']}"]
    return "\n".join(lines) + "\n"
