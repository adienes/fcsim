"""An audited solo goldfish: one agent pilots a deck against a seat that never acts.

It answers the cheap questions (how fast and how reliably does the deck do its thing) and is the
first end-to-end run of everything: deck, table, tools over MCP, agent, auditor, game directory.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from ..agents import Budget, BudgetExceeded, Runner, RunnerError, cost_summary
from ..audit import Auditor
from ..cards import CardDatabase, read_decklist, resolve, validate_deck
from ..mcp import McpServer
from ..rules import RulesReference
from ..table import Table
from ..tools import SeatTools
from .gamedir import GameDirectory

PILOT, GOLDFISH = 0, 1

SYSTEM = """\
You are playing a game of Magic: The Gathering (Commander) at a virtual table, through tools. \
This game is a solo test of your deck. The other seat, Goldfish, never does anything: it has 40 \
life, plays no cards and never blocks. Pilot the deck as a strong player would to show what it \
can do: make your land drops, develop your mana, cast your spells and your commander when it is \
sensible, and attack Goldfish when you have attackers. Sequence carefully and do not waste mana.

How the table works. The table is a tabletop, not a rules engine: it knows zones, turns and \
priority, and no card text. You carry out every rule and card effect yourself with your tools, \
honestly and exactly, and a rules auditor reviews each of your turns afterwards. Read a card \
with lookup_card before relying on what it does, and look a rule up when you are unsure.
- get_view shows everything you may see. `pen` says who the table is waiting for and for what. \
Cards are named by handles, which change whenever a card changes zones: look again after acting.
- When you cast a spell you keep priority. Pass priority to let it resolve. If you did not cast \
it as "auto_permanent", the table then waits for you to resolve it: use resolve, as many times \
as the effect needs, then finish_resolution.
- Pay costs by naming the permanents you tap in `payment`. Tap only what the cost needs.
- With an empty stack, passing priority moves the game to the next step. The table untaps your \
permanents and draws your card for the turn by itself. Do not pass one step at a time: use \
pass_priority with `until` to go straight to the step where you next want to act.
- In combat the table asks you to declare_attackers, then later to deal combat_damage.
- Carry out triggered abilities (add_trigger, then resolve them) and state-based actions yourself.
- Goldfish passes instantly, so after you pass you usually hold the pen again at once.

Each wake is one of your turns. Begin with get_view and get_notes. Before your turn ends, \
write_notes with what you are planning. When the table says your wake has ended, stop calling \
tools and reply with two or three sentences: what you did this turn and why."""


def _deck_reference(deck, database: CardDatabase) -> str:
    lines = []
    for card, count in [(c, 1) for c in deck.commanders] + list(deck.cards):
        face = card.front
        stats = f" {face.power}/{face.toughness}" if face.power is not None else ""
        text = " ".join(card.text.split())
        lines.append(f"{count} {card.name} {card.mana_cost} [{face.type_line}{stats}] {text}")
    return "\n".join(lines)


_MECHANICAL = {"priority": "pass_priority", "mulligan": "keep", "declare_attackers": "declare_attackers",
               "declare_blockers": "declare_blockers", "combat_damage": "combat_damage", "discard": "finish_step",
               "held_step": "finish_step", "resolution": "finish_resolution"}


def goldfish(deck_path: str | Path, runner: Runner, auditor_runner: Runner, database: CardDatabase, *, turns: int = 5,
             seed: int | None = None, out_root: str | Path = "games", rules: RulesReference | None = None,
             echo=lambda line: print(line, flush=True)) -> dict:
    """Play `turns` pilot turns. `runner` plays the pilot and `auditor_runner` audits; give both the
    same Budget to cap the whole run. Returns the report; everything is also written to a directory."""
    decklist = read_decklist(deck_path)
    deck = resolve(decklist, database, online=True)
    issues = validate_deck(deck, database)
    if deck.unknown or not deck.commanders:
        raise ValueError("The deck has unrecognized cards or no commander; run `fcsim deck check` first.")
    seed = seed if seed is not None else secrets.randbelow(2 ** 31)
    directory = GameDirectory(out_root, f"goldfish_{deck.commanders[0].name}", seed)
    seats = [{"name": "Pilot", "commanders": [c.name for c in deck.commanders], "library": [c.name for c in deck.library()]},
             {"name": "Goldfish", "commanders": [], "library": ["Wastes"] * 60}]
    table = Table(seats, seed=seed, first=PILOT, gated=True, on_event=directory.journal,
                  card_lookup=lambda n: (c := database.find(n)) and {"mana_value": c.mana_value})
    directory.write("decks/seat-0.txt", decklist.to_mtgo())
    directory.pin_cards({c.name for c in deck.commanders} | {c.name for c, _ in deck.cards}, database)
    directory.pin_rules(rules)
    directory.write("config.json", {"mode": "goldfish", "deck": str(deck_path), "turns": turns, "table": table.config,
                                    "pilot_model": runner.model, "auditor_model": auditor_runner.model,
                                    "pilot_backend": getattr(runner, "backend", "custom"),
                                    "auditor_backend": getattr(auditor_runner, "backend", "custom"),
                                    "deck_issues": [i.to_dict() for i in issues]})
    auditor = Auditor(auditor_runner, database, context=(
        "A solo test of Pilot's deck that stands in for a four-player Commander game, so multiplayer rules "
        "apply: the player who goes first draws on their first turn. Goldfish is a stand-in opponent that "
        "never acts; the driver passes and declares no blocks for it."))
    system = SYSTEM + "\n\nYOUR DECK (count, name, cost, type, text)\n" + _deck_reference(deck, database)
    current: dict = {}

    def needs_pilot(seat: int, kind: str) -> bool:
        """Dispatch by seat and by decision, whoever's turn it is. On Goldfish's turn the pilot's
        priority is passed for it (a goldfish plays nothing at instant speed), but a trigger, a
        question or a resolution of its own is a real decision."""
        return seat == PILOT and (kind == "mulligan" or table.game.turn.active == PILOT or kind != "priority")

    def at_end_of_turn() -> bool:
        control = table.game.control
        return control.kind == "audit" and control.detail["resume"]["do"] == "next_turn"

    def wake_is_over() -> bool:
        if current["mode"] == "decision":  # a decision outside the pilot's turn ends once it is made
            expected = table.expected()
            return table.awaiting_audit or not expected or not needs_pilot(*expected[0])
        return table.game.turn.number != current["turn"] or at_end_of_turn()

    def settle(full: bool) -> None:
        """Do everything that needs no model. Inside a wake (`full` false) this stops where the wake
        ends, leaving the table at its checkpoint so the pilot's actions are audited and their hooks
        applied before any later step runs. Between wakes it runs on to the pilot's next decision."""
        for _ in range(600):
            if current.get("wake") and wake_is_over():
                table.close_wake(current["wake"])
                current["ended_at"] = current["ended_at"] or len(table.events)
                if not full:
                    return
            if table.awaiting_audit:  # a checkpoint inside the pilot's turn, or after Goldfish acted: the turn is audited whole
                table.release()
                continue
            expected = table.expected()
            if not expected or needs_pilot(*expected[0]):
                return
            seat, kind = expected[0]
            if kind not in _MECHANICAL or "error" in table.act(seat, _MECHANICAL[kind]):
                return

    def tools_for(token: str):
        tools = current.get("tools")
        if tools is None or token != current.get("token"):
            return None

        def call(request_id: str, name: str, arguments: dict) -> dict:
            result = tools.call(request_id, name, arguments)
            settle(full=False)
            if isinstance(result, dict) and "pen" in result:
                control = table.game.control
                result["pen"] = {"kind": control.detail.get("action", control.kind), "seats": control.seats}
                result["turn"] = {"number": table.game.turn.number, "step": table.game.turn.step}
                if current["ended_at"]:
                    result["wake"] = "This wake has ended. Stop calling tools and give your summary."
            return result
        return tools.definitions, call

    report: dict = {"directory": str(directory.path), "seed": seed, "turns": [], "stopped": None,
                    "deck_issues": [issue.message for issue in issues]}
    for issue in issues:  # a playtest may use a list that is not tournament-legal, such as 104 cards mid-cut
        echo(f"note: {issue.message} Playing it as it is.")
    with McpServer(tools_for) as server:
        try:
            while table.game.control.kind != "over" and table.game.turn.number <= 2 * turns - 1:
                if table.awaiting_audit:
                    table.release()
                    continue
                expected = table.expected()
                if not expected:
                    break
                seat, kind = expected[0]
                if not needs_pilot(seat, kind):
                    before = len(table.events)
                    settle(full=True)
                    if len(table.events) == before:  # never spin: a decision nobody can make ends the run
                        report["stopped"] = f"the driver cannot act for seat {seat} ({kind})"
                        break
                    continue
                turn_number, own_turn = table.game.turn.number, table.game.turn.active == PILOT or kind == "mulligan"
                token, wake = secrets.token_hex(16), table.open_wake(PILOT, kind)
                current.update(tools=SeatTools(table, PILOT, wake, database=database, rules=rules), token=token, wake=wake,
                               turn=turn_number, ended_at=None, mode="turn" if own_turn else "decision")
                snapshot = auditor.snapshot(table)
                label = 0 if kind == "mulligan" else (turn_number + 1) // 2
                if kind == "mulligan":
                    prompt = "Your opening hand has been dealt. Look at it with get_view, then keep or mulligan."
                elif own_turn:
                    prompt = (f"It is your turn {label} (table turn {turn_number}). "
                              "Play it. Your turn ends when you have passed priority through your end step.")
                else:
                    prompt = (f"It is Goldfish's turn (table turn {turn_number}) and the table is waiting for you: {kind}. "
                              "Look with get_view, do what is needed, and stop.")
                echo(f"turn {label}: pilot is playing ({kind}{'' if own_turn else ', during Goldfish turn'}) ...")
                reply = runner.ask(system, prompt, tools=server.connection(
                    token, tuple(t["name"] for t in current["tools"].definitions)))
                table.close_wake(wake)
                forced = False
                for _ in range(80):  # it stopped early: finish its turn, or its decision, for it
                    if wake_is_over() or not table.expected() and not table.awaiting_audit:
                        break
                    forced = True
                    if table.awaiting_audit:
                        table.release()
                    else:
                        s, k = table.expected()[0]
                        if "error" in table.act(s, _MECHANICAL.get(k, "finish_step")):
                            break
                ended = current["ended_at"] or len(table.events)
                entry = {"turn": label, "table_turn": turn_number, "kind": kind, "summary": reply.text,
                         "tool_calls": len(current["tools"].calls),
                         "tool_errors": [c for c in current["tools"].calls if c["error"]],
                         "forced_to_end": forced, "seconds": round(reply.seconds, 1),
                         "estimated_cost_usd": reply.estimated_cost_usd, "events": [snapshot[2], ended]}
                directory.append("transcript-seat-0.jsonl", {**entry, "calls": current["tools"].calls})
                report["turns"].append(entry)
                if kind != "mulligan" and any(e["public"] for e in table.events[snapshot[2]:ended]):
                    echo(f"         auditing {ended - snapshot[2]} events ...")
                    verdict, audit_reply = auditor.audit(table, snapshot, ended)
                    entry["audit"] = {**verdict, "seconds": round(audit_reply.seconds, 1)}
                    if verdict.get("hooks"):
                        entry["hooks_applied"] = "error" not in _apply(table, verdict["hooks"])
                    directory.append("audits.jsonl", {"turn": label, "events": entry["events"], **verdict})
                directory.write("state.json", table.game.to_dict())
                directory.write("notes-seat-0.md", table.notes[PILOT])
                current.update(tools=None, token=None, wake=None)
        except BudgetExceeded as stop:
            report["stopped"] = str(stop)
        except RunnerError as error:
            report["stopped"] = f"model error: {error}"
    report["metrics"] = metrics(table)
    report["usage"] = runner.budget.to_dict()
    directory.write("state.json", table.game.to_dict())
    directory.write("usage.json", report["usage"])
    directory.write("report.json", report)
    directory.write("report.md", render(report, deck))
    return report


def _apply(table: Table, hooks: list[dict]) -> dict:
    try:
        return table.apply_hooks(hooks)
    except ValueError as error:  # a hook for something no longer on the battlefield
        return {"error": str(error)}


def metrics(table: Table) -> dict:
    """What the log says about the pilot's development, turn by turn."""
    by_turn: dict[int, dict] = {}
    for event in table.events:
        if event["seat"] != PILOT or event["turn"] % 2 == 0:
            continue
        row = by_turn.setdefault((event["turn"] + 1) // 2, {"lands": 0, "spells": [], "attacked": False, "warnings": 0})
        row["warnings"] += len(event["lints"])
        if event["tool"] == "play_land":
            row["lands"] += 1
        elif event["tool"] == "cast":
            row["spells"].append(re.sub(r"^.*? casts (.*?) from .*$", r"\1", event["public"][0]))
        elif event["tool"] == "declare_attackers" and event["args"].get("attacks"):
            row["attacked"] = True
    game = table.game
    pilot, fish = game.seats[PILOT], game.seats[GOLDFISH]
    return {"turns": by_turn, "goldfish_life": fish.life, "pilot_life": pilot.life, "cards_in_hand": len(pilot.hand),
            "permanents": sum(1 for i in game.battlefield if game.objects[i].controller == PILOT),
            "commander_casts": pilot.commander_casts, "mulligans": pilot.mulligans}


def render(report: dict, deck) -> str:
    m = report["metrics"]
    lines = [f"# Goldfish: {' + '.join(c.name for c in deck.commanders)}", "",
             f"Seed {report['seed']}. Mulligans: {m['mulligans']}. After the last turn: Goldfish at {m['goldfish_life']} life, "
             f"{m['permanents']} permanents, {m['cards_in_hand']} cards in hand. Commander casts: {m['commander_casts'] or 'none'}.",
             *[f"\nPlayed as listed although: {message}" for message in report.get("deck_issues", [])],
             "", "| Turn | Lands | Spells cast | Attacked | Audit | Pilot's summary |", "| --- | --- | --- | --- | --- | --- |"]
    for entry in report["turns"]:
        if entry["kind"] == "mulligan":
            continue
        row = m["turns"].get(entry["turn"], {"lands": 0, "spells": [], "attacked": False})
        audit = entry.get("audit")
        serious = [f for f in (audit or {}).get("findings", []) if f["kind"] != "note"]
        mark = "not audited" if audit is None else "ok" if not serious else "; ".join(f"{f['kind']}: {f['summary']}" for f in serious)
        summary = " ".join(entry["summary"].split()).replace("|", "/")
        lines.append(f"| {entry['turn']} | {row['lands']} | {', '.join(row['spells']) or '-'} | {'yes' if row['attacked'] else '-'} "
                     f"| {mark.replace('|', '/')} | {summary}{' (turn ended by the driver)' if entry['forced_to_end'] else ''} |")
    findings = sum(len([f for f in (e.get("audit") or {}).get("findings", []) if f["kind"] != "note"]) for e in report["turns"])
    errors = sum(len(e["tool_errors"]) for e in report["turns"])
    usage = report["usage"]
    lines += ["", "## Rules health", "",
              f"{findings} auditor finding(s) across {sum(1 for e in report['turns'] if e.get('audit'))} audited turns; "
              f"{errors} rejected tool call(s). Findings are recorded, not corrected: the correction flow is not built yet, "
              "so a finding may mean later turns played on from a wrong board.",
              "", "## Usage", "",
              f"{usage['calls']} model calls, {usage['seconds']:.0f} seconds of model time, "
              f"{cost_summary(usage)}."]
    if report["stopped"]:
        lines += ["", f"**Stopped early:** {report['stopped']}"]
    return "\n".join(lines) + "\n"
