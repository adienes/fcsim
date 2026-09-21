"""A multiplayer game: one agent per seat, an auditor behind them, a directory of what happened.

The scheduler is simple on purpose. It wakes whichever seat the table is waiting for, passes
for a seat that has priority on someone else's turn with nothing on the stack, audits at every
checkpoint where something auditable happened, and turns the auditor's findings into waiting
triggers and correction wakes. Narrower watches, triage and disputes come later (PLAN.md §6, §8).
"""

from __future__ import annotations

import json
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..agents import BudgetExceeded, Runner, RunnerError, cost_summary

from ..audit import SERIOUS, Auditor, Judge, describe
from ..audit.auditor import card_reference
from ..cards import CardDatabase, read_decklist, resolve, validate_deck
from ..mcp import McpServer
from ..rules import RulesReference
from ..table import Table
from ..table.ops import _named as ops_named
from ..tools import SeatTools
from . import rubric
from .gamedir import GameDirectory, table_facts
from .goldfish import _deck_reference

SYSTEM = """\
You are {name}, one of {count} players in a game of Magic: The Gathering (Commander) at a virtual \
table, played through tools. Play to win, as a strong and fair player would: develop your board, \
judge who is the real threat, hold interaction for what matters, attack where it helps you most. \
Talk to the table with `say` when it serves you: make deals, point at threats, bluff about what \
you hold. Deals are not binding. If you name the players you are speaking to in `to`, they answer \
at once and their answers come back in the result, so you can settle something before you act. Never misstate the public game state or the rules.

How the table works. The table is a tabletop, not a rules engine: it knows zones, turns and \
priority, and no card text. You carry out every rule and card effect yourself with your tools, \
honestly and exactly, and a rules auditor checks each stretch of play. If it finds an error of \
yours you will be asked to correct it; if you are sure it is the auditor that is wrong, `contest` \
the finding instead and a judge decides. If another player breaks a rule in a way that matters, \
raise it with `dispute`. Read a card with lookup_card before relying on what it \
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
for you: the result then says `wake`. When you end your turn tapped out, or hold nothing you would \
respond with, call set_response_policy so the table stops asking you until your next turn. On \
other players' turns you are asked whether you respond when something goes on the stack, when \
attackers are declared and when blocks are; at other moments the table passes for you, except that \
set_response_policy with hold_for_end_step gives you priority in the end step before your turn, \
for an instant or a flash card you are holding for it. Then stop calling \
tools and reply with one or two sentences on what you did and why; only you will see them."""

_TRIAGE = {"type": "object", "additionalProperties": False, "required": ["respond", "ask_again", "why"],
           "properties": {"respond": {"type": "boolean"}, "ask_again": {"type": "boolean"},
                          "policy": {"type": "string", "enum": ["ask", "pass_unless_targeted", "pass_always"]},
                          "why": {"type": "string", "description": "One short sentence."}}}
_OPENING = {"type": "object", "additionalProperties": False, "required": ["keep", "bottom", "why"],
            "properties": {"keep": {"type": "boolean"}, "why": {"type": "string", "description": "One short sentence."},
                           "bottom": {"type": "array", "items": {"type": "string"},
                                      "description": "Handles of the cards to put on the bottom of your library, if you keep after more than one mulligan."}}}
_REPLY = {"type": "object", "additionalProperties": False, "required": ["reply"],
          "properties": {"reply": {"type": "string", "description": "What you say back, in a sentence or two; empty to stay silent."}}}
_REPLIES_PER_WAKE = 3
_WINDOW = {"type": "object", "additionalProperties": False, "required": ["act", "why"],
           "properties": {"act": {"type": "boolean"}, "why": {"type": "string", "description": "One short sentence."}}}
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


def _lookup(database: CardDatabase):
    def lookup(name: str) -> dict | None:
        card = database.find(name)
        return card and table_facts(card)
    return lookup


def _names(decks: list) -> list[str]:
    names = []
    for index, deck in enumerate(decks):
        name = deck.commanders[0].name.split(",")[0].split(" the ")[0]
        names.append(name if name not in names else f"{name} {index}")
    return names


def play_pod(deck_paths: list[str | Path], runners: list[Runner], auditor_runner: Runner, database: CardDatabase, *, cycles: int = 3,
             seed: int | None = None, out_root: str | Path = "games", rules: RulesReference | None = None,
             resume: str | Path | None = None, audit_every: str = "turn", triage_runners: list[Runner] | None = None,
             refresh_before_blocks: bool = True, judge_runner: Runner | None = None, hook_library: str | Path | None = None,
             echo=lambda line: print(line, flush=True)) -> dict:
    """Play up to `cycles` turns per seat. `runners[i]` pilots seat i; share one Budget across all
    runners and the auditor to cap the whole game.

    With `resume`, play `cycles` more rounds of the game in that directory: its journal is replayed
    into a table, the decks are the copies it kept, and players pick up from their notes.
    `audit_every` is "turn" (one audit of each whole turn, before the next begins) or "checkpoint"
    (an audit every time the pen moves after an action: stricter and several times slower).
    `triage_runners[i]` answers seat i's respond-or-pass questions, if given: a cheaper or
    lower-effort runner than the one that plays the seat. With `refresh_before_blocks` the auditor
    works out current power and toughness before blockers are chosen, if the battlefield has
    changed since it last did. `judge_runner` rules when a player contests an auditor's finding;
    by default the auditor's runner does, as a separate call with the judge's own brief.
    `hook_library` is a JSON file of what auditors have written for each card in earlier games: a card
    in it has its triggers and holds from the moment it arrives, and the file learns from this game."""
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
                                           card_lookup=_lookup(database)),
                     earlier.read("config.json")["seats"], decks, runners, auditor_runner, database, cycles, rules, echo,
                     earlier=earlier.read("report.json") if (earlier.path / "report.json").is_file() else {"wakes": []},
                     audit_every=audit_every, triage_runners=triage_runners, refresh_before_blocks=refresh_before_blocks,
                     judge_runner=judge_runner, hook_library=hook_library)
    seed = seed if seed is not None else secrets.randbelow(2 ** 31)
    directory = GameDirectory(out_root, "pod_" + "-".join(names), seed)
    table = Table([{"name": name, "commanders": [c.name for c in deck.commanders], "library": [c.name for c in deck.library()]}
                   for name, deck in zip(names, decks, strict=True)], seed=seed,
                  gated=True if audit_every == "checkpoint" else "turn", on_event=directory.journal,
                  card_lookup=_lookup(database))
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
                 audit_every=audit_every, triage_runners=triage_runners, refresh_before_blocks=refresh_before_blocks,
                 judge_runner=judge_runner, hook_library=hook_library)


def _play(directory: GameDirectory, table: Table, names: list[str], decks: list, runners: list, auditor_runner, database: CardDatabase,
          cycles: int, rules: RulesReference | None, echo, *, earlier: dict | None = None, audit_every: str = "turn",
          triage_runners: list | None = None, refresh_before_blocks: bool = True, judge_runner=None,
          hook_library: str | Path | None = None) -> dict:
    """Schedule seats and audits until the game ends or `cycles` more rounds have been played."""
    count, seed = len(names), table.config["seed"]
    table.on_event = directory.journal  # a replayed table was built without its journal
    # A game's length is counted in rounds, each living player's own turns, so that eliminations do
    # not shorten it. On a resume the turn in progress (or just begun) is the first of the new rounds.
    game = table.game
    last_round = (game.seats[game.turn.active].turns_taken - 1 if earlier is not None else 0) + cycles
    context = f"A {count}-player Commander game. Seats, in turn order: " + ", ".join(f"{name} (seat {i})" for i, name in enumerate(names)) + "."
    auditor = Auditor(auditor_runner, database, context=context)
    judge = Judge(judge_runner or auditor_runner, database, rules=rules, context=context)
    systems = [SYSTEM.format(name=name, count=count) + "\n\nYOUR DECK (count, name, cost, type, text)\n"
               + _deck_reference(deck, database) for name, deck in zip(names, decks, strict=True)]
    current: dict = {}
    # Each seat's place in the log, so a wake can say what happened since its last one. After a
    # resume the players start from their notes and the board, not from the whole history again.
    seen = [len(table.events) if earlier is not None else 0] * count
    decisions: dict = {}  # who said they will respond to the stack as it currently stands
    waived: list[set] = [set() for _ in range(count)]  # per seat, the stack items it has said it will let resolve
    # What the auditor has written for each card so far, relative to its controller, so that another
    # copy of it, or the same card coming back, has its hooks from the moment it arrives and not from
    # the audit a turn later. In the second pod Wilhelt died, was recast, and missed his end-step trigger.
    library = Path(hook_library) if hook_library else None
    known_hooks: dict[str, list[dict]] = json.loads(library.read_text(encoding="utf-8")) if library and library.is_file() else {}
    hooked: set[int] = set()
    for entry in table.game.ledger:  # a resumed game already has a ledger to learn from
        source = table.game.objects.get(entry.get("source"))
        if source is not None:
            hooked.add(source.id)
            known_hooks.setdefault(source.name, []).append(_relative(entry, source.controller))
    windows: dict = {}  # (table turn, step) -> the seats that said they would act in that empty-stack window
    stamped: list = [None]  # what the battlefield looked like when power and toughness were last worked out
    report: dict = {"directory": str(directory.path), "seed": seed, "seats": names, "stopped": None,
                    "rulings": list((earlier or {}).get("rulings", [])), "talk": list((earlier or {}).get("talk", [])),
                    "triage": list((earlier or {}).get("triage", [])), "refreshes": list((earlier or {}).get("refreshes", [])),
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
            if name == "say" and result.get("ok") and arguments.get("to") and current["replies"] < _REPLIES_PER_WAKE:
                current["replies"] += 1
                try:
                    result = {**result, "replies": replies(me, str(arguments.get("text", "")), arguments["to"])}
                except RunnerError:
                    pass  # talk is a courtesy: a failed question must not cost the speaker its turn
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
                              "carry_on": f"Nobody else acted and the table is waiting for you again: {_WHY.get(why, why)}{_arithmetic(table, me)} "
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
                          "", "BY THE TABLE'S COUNT your untapped mana sources can pay for: " + (", ".join(table.affordable(seat)) or "nothing")
                          + " (one at a time; before any cost reducers, extra costs, Treasures or X).",
                          *(["", "YOUR TRIGGERS WAITING TO GO ON THE STACK", *waiting] if waiting else []),
                          *(["", "QUESTIONS FOR YOU", *asked] if asked else []),
                          *(["", "THE JUDGE'S RULINGS IN THIS GAME (they bind everyone, the auditor included)",
                             *(f"- {r['text']}" for r in view["rulings"])] if view["rulings"] else []),
                          "", "CARDS IN PLAY", card_reference([n for n in in_play if n], database, rulings_per_card=0) or "(none)",
                          "", "YOUR NOTES", table.notes[seat] or "(none yet)"])

    def news(seat: int) -> str:
        events = table.act(seat, "get_log", {"since": seen[seat]})["events"]
        lines = [f"[{e['step']}] " + " ".join(e["text"]) + (" (to you: " + " ".join(e["private"]) + ")" if e.get("private") else "")
                 for e in events if (e["text"] and e["tool"] != "pass_priority") or e.get("private")][-40:]  # passes are noise
        return "Since your last wake:\n" + "\n".join(lines) if lines else "Nothing has happened since your last wake."

    def triage(seats: list[int]) -> dict[int, bool]:
        """Ask several seats at once, each with one question and no tools, whether they want to
        respond to what is on the stack. Most answers are no, and a no costs seconds, not a wake.
        One answer can cover the whole stack, and the rest of the turn, so that a chain of
        triggers is one question and not one per trigger."""
        game = table.game
        top = game.stack[-1]

        def question(seat: int) -> str:
            items = [f"  {n}. {names[item.controller]}'s {item.source_name}" + (f": {item.text}" if item.text else "")
                     + ("" if item.id in waived[seat] else "  (new since you were last asked)")
                     for n, item in enumerate(reversed(game.stack), 1)]
            return (f"{names[top.controller]}'s {top.source_name} is on top of the stack and you have priority. The stack, top first:\n"
                    + "\n".join(items) + "\nDo you want to respond now, before the top item resolves, by casting a spell or "
                    "activating an ability? Answer respond=false to pass; answer respond=true only if you would actually act "
                    "now, and you will then be woken to do it. If you pass, `ask_again` says what happens as this stack "
                    "resolves: false means you will let everything now on the stack resolve, and the table passes for you "
                    "until something new is added; true means you want to be asked again before each lower item resolves. "
                    "If you would respond to nothing at all before your next turn (you are tapped out, or hold nothing you "
                    "could use), also set `policy` to \"pass_always\", or to \"pass_unless_targeted\" to be asked only "
                    "about things that target you or what you control.")
        prompts = {seat: f"{when()} {question(seat)}\n\n"
                         f"{news(seat)}\n\n{briefing(seat)}" for seat in seats}

        def one(seat: int):
            return seat, (triage_runners or runners)[seat].ask(systems[seat], prompts[seat], schema=_TRIAGE)
        with ThreadPoolExecutor(max_workers=len(seats)) as pool:
            replies = list(pool.map(one, seats))
        answers = {}
        for seat, reply in replies:
            data = reply.data or {}
            answers[seat] = bool(data.get("respond"))
            if not answers[seat]:  # asked again by default: a player who says nothing about the rest has only passed once
                waived[seat] = {top.id} if data.get("ask_again", True) else {item.id for item in game.stack}
                if data.get("policy") in ("pass_unless_targeted", "pass_always"):
                    table.act(seat, "set_response_policy", {"policy": data["policy"]})
            record = {"turn": game.turn.number, "seat": seat, "respond": answers[seat], "why": data.get("why", ""),
                      "about": top.source_name, "stack": len(game.stack), "ask_again": bool(data.get("ask_again", True)),
                      "policy": data.get("policy"), "seconds": round(reply.seconds, 1)}
            report["triage"].append(record)
            directory.append(f"transcript-seat-{seat}.jsonl", {"kind": "triage", **record})
        echo(f"turn {game.turn.number} {game.turn.step}: response to {top.source_name}: "
             + ", ".join(f"{names[s]} {'RESPONDS' if a else 'passes'}" for s, a in answers.items()))
        return answers

    def opening_hands(server: McpServer) -> None:
        """Every seat decides on its opening hand at the same time, with one question and no tools:
        the decisions are independent, and four sessions one after another took minutes before turn 1.
        A seat whose answer the table refuses gets an ordinary wake instead."""
        while table.game.control.kind == "setup":
            deciding = list(table.game.control.seats)

            def question(seat: int) -> str:
                taken = table.game.seats[seat].mulligans
                return (f"The game is about to begin. You are number {table.game.turn_order_from(table.game.turn.active).index(seat) + 1} of "
                        f"{count} in turn order. This is your opening hand, after {taken} mulligan(s). Keep it (keep=true) or mulligan "
                        "(keep=false): a mulligan shuffles it away and draws seven new cards. The first mulligan is free; for each one "
                        f"after that you put a card on the bottom when you keep. If you keep this hand you owe {max(0, taken - 1)}: list "
                        f"exactly that many handles in `bottom`.\n\n{briefing(seat)}")
            prompts = {seat: question(seat) for seat in deciding}  # built here: the card database is not for several threads at once

            def one(seat: int):
                return seat, runners[seat].ask(systems[seat], prompts[seat], schema=_OPENING)
            with ThreadPoolExecutor(max_workers=len(deciding)) as pool:
                replies = list(pool.map(one, deciding))
            for seat, reply in replies:
                data, started = reply.data or {}, len(table.events)
                result = table.act(seat, "mulligan") if data.get("keep") is False else table.act(seat, "keep", {"bottom": data.get("bottom") or []})
                echo(f"turn 0 setup: {names[seat]} {'mulligans' if data.get('keep') is False else 'keeps'}: {data.get('why', '')}")
                entry = {"seat": seat, "turn": 0, "kind": "mulligan", "summary": data.get("why", ""), "events": [started, len(table.events)],
                         "tool_calls": 0, "tool_errors": [], "forced": None, "seconds": round(reply.seconds, 1),
                         "estimated_cost_usd": reply.estimated_cost_usd}
                report["wakes"].append(entry)
                directory.append(f"transcript-seat-{seat}.jsonl", {**entry, "calls": []})
                if "error" in result:
                    wake(server, seat, "mulligan")
                    if seat in table.game.control.seats and len(table.events) == started:
                        # Still undecided, and nothing it tried was accepted: keep for it, so that a game always starts.
                        hand = table.view(seat)["seats"][seat]["hand"]["cards"]
                        table.act(seat, "keep", {"bottom": [c["handle"] for c in hand[:max(0, table.game.seats[seat].mulligans - 1)]]})
                        report["wakes"][-1]["forced"] = "the driver kept the hand and chose what went on the bottom"
                seen[seat] = len(table.events)

    def replies(speaker: int, text: str, to: list) -> list[dict]:
        """A player has spoken to others by name. Each is asked at once, with no tools, what it says
        back, so that a deal can be struck before the speaker acts and not a round later."""
        game = table.game
        asked = [s for s in dict.fromkeys(to) if isinstance(s, int) and 0 <= s < count and s != speaker and game.seats[s].status == "alive"]
        prompts = {seat: (f"{when()} {names[speaker]} has just said to you, at the table: \"{text}\" You are not acting now, only "
                          "talking. Say what serves you: agree, refuse, bargain, bluff, point at the real threat. Deals are not "
                          f"binding. Leave `reply` empty to stay silent.\n\n{news(seat)}\n\n{briefing(seat)}") for seat in asked}

        def one(seat: int):
            return seat, runners[seat].ask(systems[seat], prompts[seat], schema=_REPLY)
        if not asked:
            return []
        with ThreadPoolExecutor(max_workers=len(asked)) as pool:
            answers = list(pool.map(one, asked))
        said = []
        for seat, reply in answers:
            words = str((reply.data or {}).get("reply", "")).strip()
            record = {"turn": game.turn.number, "seat": seat, "to": speaker, "asked": text, "reply": words, "seconds": round(reply.seconds, 1)}
            report["talk"].append(record)
            directory.append(f"transcript-seat-{seat}.jsonl", {"kind": "reply", **record})
            if words and "error" not in table.act(seat, "say", {"text": words, "to": [speaker]}):
                said.append({"from": names[seat], "seat": seat, "text": words})
        echo(f"turn {game.turn.number} {game.turn.step}: {names[speaker]} spoke to {', '.join(names[s] for s in asked)}; "
             f"{len(said)} answered")
        return said

    def window(seats: list[int], situation: str) -> set[int]:
        """Ask several seats at once whether they would act now, with the stack empty on another player's
        turn. Without this nobody could ever kill an attacker, play a combat trick after blocks, or
        flash something in at the end of turn: the driver passes for everyone when nothing is on the stack."""
        game = table.game
        question = (f"It is {names[game.turn.active]}'s {game.turn.step} step, the stack is empty, and you have priority. {situation} Do you "
                    "want to act now, at instant speed: cast an instant or a card with flash, or activate an ability? Answer "
                    "act=false to pass. Answer act=true only if you would actually do it now; you will then be woken to do it.")
        prompts = {seat: f"{when()} {question}\n\n{news(seat)}\n\n{briefing(seat)}" for seat in seats}

        def one(seat: int):
            return seat, (triage_runners or runners)[seat].ask(systems[seat], prompts[seat], schema=_WINDOW)
        with ThreadPoolExecutor(max_workers=len(seats)) as pool:
            replies = list(pool.map(one, seats))
        acting = set()
        for seat, reply in replies:
            data = reply.data or {}
            acting |= {seat} if data.get("act") else set()
            record = {"turn": game.turn.number, "seat": seat, "respond": bool(data.get("act")), "why": data.get("why", ""),
                      "about": f"{game.turn.step} window", "stack": 0, "seconds": round(reply.seconds, 1)}
            report["triage"].append(record)
            directory.append(f"transcript-seat-{seat}.jsonl", {"kind": "triage", **record})
        echo(f"turn {game.turn.number} {game.turn.step}: a chance to act: "
             + ", ".join(f"{names[s]} {'ACTS' if s in acting else 'passes'}" for s, _ in replies))
        return acting

    def wake(server: McpServer, seat: int, kind: str, extra: str = "") -> None:
        token, handle = secrets.token_hex(16), table.open_wake(seat, kind)
        current.update(tools=SeatTools(table, seat, handle, database=database, rules=rules), token=token, wake=handle,
                       seat=seat, ended=False, replies=0)
        game = table.game
        windows.get((game.turn.number, game.turn.step), set()).discard(seat)
        if kind == "correction" and not extra:
            extra = _correction(game.control.detail)
        prompt = f"{when()} {extra or _WHY.get(kind, 'The table is waiting for you.')}{_arithmetic(table, seat)}\n\n{news(seat)}\n\n{briefing(seat)}"
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

        def needs_no_audit(event: dict) -> bool:
            """Talk, passing, and a basic land played when the table allows it: nothing there for a rules check to find."""
            if event["seat"] is None or not event["public"] or event["tool"] in ("pass_priority", "say", "write_notes", "keep", "mulligan"):
                return True
            played = re.search(r" plays (.+) from .+'s hand\.$", event["public"][0]) if event["tool"] == "play_land" and not event["lints"] else None
            card = database.find(played[1]) if played else None
            return card is not None and card.is_basic
        if all(needs_no_audit(e) for e in unit):
            return
        actions = sum(1 for e in unit if e["seat"] is not None and e["tool"] not in ("pass_priority", "say", "write_notes", "set_response_policy"))
        echo(f"         auditing {actions} actions ...")
        verdict, reply = auditor.audit(table, snapshot)
        record = {"turn": table.game.turn.number, "events": [snapshot[2], len(table.events)], **verdict, "seconds": round(reply.seconds, 1)}
        report["audits"].append(record)
        directory.append("audits.jsonl", record)
        if verdict.get("hooks"):
            try:
                table.apply_hooks(verdict["hooks"])
            except ValueError as error:
                record["hooks_error"] = str(error)
            fresh: dict[str, list[dict]] = {}
            for entry in table.game.ledger:
                source = table.game.objects.get(entry.get("source"))
                if source is not None:
                    hooked.add(source.id)
                    fresh.setdefault(source.name, []).append(_relative(entry, source.controller))
            known_hooks.update(fresh)
            if library and fresh:  # other games may be writing too: merge into what is on disk now, and swap the file in whole
                on_disk = json.loads(library.read_text(encoding="utf-8")) if library.is_file() else {}
                library.parent.mkdir(parents=True, exist_ok=True)
                scratch = library.with_suffix(f".{secrets.token_hex(4)}.tmp")
                scratch.write_text(json.dumps({**on_disk, **fresh}, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                scratch.replace(library)
        table.apply_computed(verdict.get("computed") or [])
        stamped[0] = _characteristics_print(table)
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
        for answer in verdict.get("disputes") or []:
            raised = table.events[answer["event"]] if 0 <= answer.get("event", -1) < len(table.events) else None
            if raised is not None and raised["tool"] == "dispute":
                table.answer_dispute(raised["seat"], answer["upheld"], answer["answer"])
        for seat, problems in at_fault.items():
            table.open_correction(seat, " | ".join(problems))
            wake(server, seat, "correction")
            if table.game.control.kind == "dispute":
                adjudicate(snapshot[2])
                if table.game.control.kind == "correction":
                    wake(server, seat, "correction")  # the judge agreed there was a problem: no appeal this time
            if table.game.control.kind == "correction":
                table.act(seat, "finish_step")

    def adjudicate(since: int) -> None:
        """A player has contested a finding. In the first real game nobody could, and two players gave
        up a card each to a false alarm about the free mulligan."""
        detail = dict(table.game.control.detail)
        echo(f"         {names[detail['seat']]} contests the finding; the judge is ruling ...")
        decision, reply = judge.rule(table, since)
        table.rule(decision["decision"], decision["explanation"], ruling=decision.get("ruling", ""), fix=decision.get("fix", ""))
        record = {"kind": "ruling", "turn": table.game.turn.number, "seat": detail["seat"], "finding": detail["finding"],
                  "objection": detail["objection"], **decision, "seconds": round(reply.seconds, 1)}
        report["rulings"].append(record)
        directory.append("audits.jsonl", record)
        echo(f"         the judge: {decision['decision']}. {decision['explanation']}")

    def refresh() -> None:
        """Work the creatures' power and toughness out again. Audits are a turn apart, and blocks are
        chosen on these numbers: in the first real game a 4/4 was blocked and dealt damage as a 3/3."""
        echo("         working out power and toughness before blocks ...")
        computed, reply = auditor.characteristics(table)
        table.apply_computed(computed)
        stamped[0] = _characteristics_print(table)
        record = {"turn": table.game.turn.number, "kind": "characteristics", "computed": computed, "seconds": round(reply.seconds, 1)}
        report["refreshes"].append(record)
        directory.append("audits.jsonl", record)

    def rehook() -> None:
        """Give a permanent that has just arrived the hooks the auditor wrote for that card before."""
        game = table.game
        for obj in [game.objects[i] for i in game.battlefield if i not in hooked]:
            hooked.add(obj.id)
            if obj.face_down or obj.name not in known_hooks:
                continue
            hooks = [{**{k: (obj.controller if v == "its controller" else v) for k, v in hook.items()},
                      "handle": table.handles[None].of("c", obj.id)} for hook in known_hooks[obj.name]]
            try:
                table.apply_hooks(hooks)
            except ValueError:
                pass  # the next audit writes them afresh

    def settle(me: int | None = None) -> str:
        """Do everything that needs no full wake: release mid-turn checkpoints, pass for seats with
        nothing to respond to, ask the rest whether they respond. Returns why it stopped: "over",
        "stuck", "audit" (the table waits for one), "wake" (some seat must decide), or "yours" when
        called from inside seat `me`'s wake and the pen has come back to it."""
        while True:
            rehook()
            game = table.game
            if game.control.kind == "over" or game.seats[game.turn.active].turns_taken > last_round:
                return "over"
            lost = _must_lose(table)
            if lost and me is not None:
                return "wake"  # end this wake first: correcting the table kills open wakes
            if lost:
                # Losing the game is a state-based action the table can see for itself, and nobody
                # should receive priority first (rule 704.3). In the first real game two players sat
                # at -1 life while priority went round.
                table.patch([{"op": "eliminate", "seat": seat, "reason": reason} for seat, reason in lost],
                            "state-based action: " + "; ".join(f"{names[seat]} loses ({reason})" for seat, reason in lost))
                continue
            if table.awaiting_audit:
                # By default a turn is audited whole, before the next one begins: that is when its
                # hooks must exist. Checkpoints inside the turn are released at once.
                if audit_every == "checkpoint" or game.control.detail["resume"]["do"] == "next_turn":
                    return "audit"
                table.release()
                continue
            if game.control.kind == "dispute":
                return "dispute"  # only after a resume: a ruling normally follows the contest at once
            expected = table.expected()
            if not expected:
                return "stuck"
            seat, kind = expected[0]
            quiet_step = seat == game.turn.active and game.turn.step in ("upkeep", "draw")
            if kind == "priority" and not game.stack and seat != game.turn.active:
                # An empty stack on someone else's turn. Usually there is nothing to do and the driver
                # passes; at the moments that matter the seats that could act are asked first, together.
                key = (game.turn.number, game.turn.step)
                if key not in windows:
                    waiting = [s for s in game.turn_order_from(seat) if s not in game.turn.passed]
                    situation = {s: _window(table, s) for s in waiting}
                    asked = [s for s in waiting if situation[s] and s != me]
                    windows.clear()
                    windows[key] = (window(asked, next(situation[s] for s in asked)) if asked else set()) | ({me} if situation.get(me) else set())
                if seat in windows[key]:
                    if seat != me:
                        return "wake"  # its wake takes it off the list: if it passes after all, the window is over for it
                    windows[key].discard(seat)
                    return "yours"
            if kind == "priority" and not game.stack and (quiet_step or seat != game.turn.active):
                # Nothing to respond to on someone else's turn; and nothing waiting in one's own
                # upkeep or draw step, or the table would be asking for a trigger, not priority. That
                # holds for the seat whose wake this is too: in the second real game the last player
                # to keep was told it had priority, in its upkeep, and played a land there.
                table.act(seat, "pass_priority")
                continue
            if seat == me:
                return "yours"
            if kind in ("priority", "declare_blockers") and _cannot_act(table, seat, kind):
                table.act(seat, _DEFAULTS[kind])  # an empty hand and nothing untapped; or nothing that could block
                continue
            if kind == "combat_damage" and seat != me and _plain_combat(table):
                # Nothing has happened since the attack that could change how damage is dealt, and the sums
                # are the table's: no session is opened for the attacker to say yes to them.
                if "error" not in table.act(seat, "combat_damage", {"as_proposed": True}):
                    continue
            if kind == "declare_blockers" and refresh_before_blocks and stamped[0] != _characteristics_print(table):
                if me is not None:
                    return "wake"  # the attacker's wake is over either way: somebody has to choose blocks
                refresh()
                continue
            if kind == "priority" and game.stack:
                # A chance to respond. Ask everyone still to act at once, except those who have
                # already said they will let the top item resolve, or have a standing "pass for me".
                signature, top = tuple(item.id for item in game.stack), game.stack[-1].id
                if decisions.get("stack") != signature:
                    decisions.clear()
                    decisions.update(stack=signature, respond=set())
                waiting = [s for s in game.turn_order_from(seat) if s not in game.turn.passed and s != me]
                asked = [s for s in waiting if s not in decisions["respond"] and top not in waived[s] and _wants_asking(table, s)]
                if asked:
                    decisions["respond"] |= {s for s, responds in triage(asked).items() if responds}
                if seat not in decisions["respond"]:
                    table.act(seat, "pass_priority")
                    seen[seat] = len(table.events)
                    continue
            return "wake"

    with McpServer(tools_for) as server:
        try:
            opening_hands(server)
            snapshot = auditor.snapshot(table)
            while True:
                why = settle()
                if why in ("over", "stuck"):
                    break
                if why == "dispute":
                    adjudicate(snapshot[2])
                    continue
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
    if count > 1:  # after the report is safe on disk: the scores need a replay of the whole journal
        report["rubric"] = rubric.measure(directory.path)
        directory.write("rubric.json", report["rubric"])
        directory.write("report.json", report)
        directory.write("report.md", render(report))
    return report


def _must_lose(table: Table) -> list[tuple[int, str]]:
    """Living seats that the rules say have lost, unless the ledger holds their loss (a "can't lose" effect)."""
    game, lost = table.game, []
    held = {e.get("seat") for e in game.ledger if e.get("kind") == "hold" and e.get("step") == "loss"}
    for seat in game.seats:
        if seat.status != "alive" or seat.index in held:
            continue
        lethal = next((f"21 or more commander damage from {source}" for source, n in seat.commander_damage.items() if n >= 21), None)
        reason = f"{seat.life} life, rule 704.5a" if seat.life <= 0 else \
            f"{seat.counters['poison']} poison counters, rule 704.5c" if seat.counters.get("poison", 0) >= 10 else \
            "drew from an empty library, rule 704.5b" if seat.drew_from_empty else lethal
        if reason:
            lost.append((seat.index, reason))
    return lost


def _arithmetic(table: Table, seat: int) -> str:
    """What the table has worked out for the combat damage step it is waiting on, for the prompt of the player who deals it."""
    proposal = table.proposed_damage(seat)
    if proposal is None:
        return ""
    return (" THE TABLE'S ARITHMETIC for this " + ("first-strike " if proposal["first_strike_step"] else "") + "damage step, from "
            + " and ".join(proposal["basis"]) + ": " + "; ".join(proposal["lines"]) + ". Call combat_damage with as_proposed true to deal "
            "exactly this, unless a card changes how this combat's damage works"
            + (f" or you want to divide the damage of {', '.join(proposal['needs_your_choice'])} among its blockers differently"
               if proposal["needs_your_choice"] else "") + ".")


def _plain_combat(table: Table) -> bool:
    """True when the table's arithmetic can simply be applied: it has the numbers, no attacker's damage
    needs dividing, nothing was cast, activated or resolved since attackers were declared, no creature
    in combat carries a note, and nothing on the ledger speaks of damage, prevention or protection."""
    game = table.game
    proposal = table.proposed_damage(None)
    if proposal is None or proposal["needs_your_choice"]:
        return False
    declared = next((e["n"] for e in reversed(table.events) if e["tool"] == "declare_attackers" and e["turn"] == game.turn.number), None)
    if declared is None or any(e["tool"] in ("cast", "activate", "add_trigger", "resolve", "act", "finish_resolution") for e in table.events[declared + 1:]):
        return False
    fighting = {a["attacker"] for a in game.turn.attackers} | {b["blocker"] for b in game.turn.blockers}
    if any(game.objects[i].annotations for i in fighting if i in game.objects):
        return False
    return not any(re.search(r"damage|prevent|protection|can't be blocked|assign", str(e.get("text", "")), re.I)
                   for e in game.ledger if e.get("kind") == "static")


def _relative(entry: dict, controller: int) -> dict:
    """A ledger entry as a hook that can be written again for another copy of the card: whoever
    controlled the permanent is remembered as "its controller", not as a seat number."""
    hook = {k: v for k, v in entry.items() if k != "source"}
    return {k: ("its controller" if k in ("controller", "seat", "whose") and v == controller else v) for k, v in hook.items()}


def _correction(detail: dict) -> str:
    """What a seat is told when the pen is handed to it to put the table right."""
    if detail.get("final"):
        return ("You contested the auditor's finding and the judge has ruled that there is something to correct. The ruling is "
                "final. Put the table right with `act` (say what you are correcting in `because`), then call finish_step.\n- "
                + detail["finding"])
    return ("The rules auditor found a problem with what you did. If it is right, put the table right with `act` (say what you "
            "are correcting in `because`), then call finish_step. If you are sure it is wrong, do not change anything: call "
            "`contest` with your reason, quoting the card or the rule, and an independent judge will rule. The auditor does "
            "raise false alarms, and a wrong correction damages the game as much as a missed one.\n"
            + "\n".join(f"- {problem}" for problem in detail["finding"].split(" | ")))


def _characteristics_print(table: Table) -> tuple:
    """Everything on the battlefield that can bear on a creature's power and toughness, as far as a
    card-blind table can tell: which permanents are there and under whom, their counters, notes and
    attachments, and the continuous effects on the ledger. Tapping and damage do not."""
    game = table.game
    return (tuple((o.id, o.controller, tuple(sorted(o.counters.items())), tuple(str(a) for a in o.annotations), o.attached_to,
                   o.face_down, o.transformed, o.phased_out) for o in (game.objects[i] for i in game.battlefield)),
            tuple(sorted(str(e.get("text")) for e in game.ledger if e.get("kind") == "static")))


def _window(table: Table, seat: int) -> str | None:
    """Whether this is a moment at which `seat` should be asked if it acts though the stack is empty,
    on another player's turn: an attack, blocks, or the end step it said it is holding something for.
    Returns what to tell it about the moment."""
    game = table.game
    turn, me = game.turn, game.seats[seat]
    mine = [game.objects[i] for i in game.battlefield if game.objects[i].controller == seat]
    if me.response_policy == "pass_always" or not (me.hand or any(not obj.tapped for obj in mine)):
        return None

    def named(target: dict) -> str:
        return game.seats[target["seat"]].name if "seat" in target else ops_named(game.objects[target["obj"]]) if target.get("obj") in game.objects else "?"
    attacks = [a for a in turn.attackers if a["attacker"] in game.objects]
    if turn.step == "declare_attackers" and attacks:
        at_me = any(a["target"].get("seat") == seat or (a["target"].get("obj") in game.objects and game.objects[a["target"]["obj"]].controller == seat)
                    for a in attacks)
        if me.response_policy == "pass_unless_targeted" and not at_me:
            return None
        return "Attackers have been declared: " + "; ".join(f"{ops_named(game.objects[a['attacker']])} at {named(a['target'])}" for a in attacks) \
            + ". Blockers have not been chosen yet."
    blocks = [b for b in turn.blockers if b["blocker"] in game.objects and b["attacker"] in game.objects]
    if turn.step == "declare_blockers" and blocks and me.response_policy == "ask":
        return "Blockers have been declared: " + "; ".join(f"{ops_named(game.objects[b['blocker']])} blocks {ops_named(game.objects[b['attacker']])}"
                                                           for b in blocks) + ". Combat damage comes next."
    order = game.turn_order_from(turn.active)
    if turn.step == "end" and me.holds_for_end_step and len(order) > 1 and order[1] == seat:
        return "This is the end step of the turn before yours, which you said you were holding something for."
    return None


def _wants_asking(table: Table, seat: int) -> bool:
    """Whether to ask this seat if it responds to the top of the stack, given its standing policy."""
    game = table.game
    policy = game.seats[seat].response_policy
    if policy == "pass_unless_targeted":
        return any(t.get("seat") == seat or (t.get("obj") in game.objects and game.objects[t["obj"]].controller == seat)
                   for t in game.stack[-1].targets)
    return policy != "pass_always"


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
    rulings = report.get("rulings", [])
    lines += ["", "## Rules health", "",
              f"{len(report['audits'])} audits; {len(serious)} finding(s); "
              + (f"{len(rulings)} contested ({', '.join(f'{n} {d}' for d in ('uphold', 'overrule', 'amend') if (n := sum(1 for r in rulings if r['decision'] == d)))}); " if rulings else "") +
              f"{sum(len(w['tool_errors']) for w in report['wakes'])} rejected tool call(s); "
              f"{sum(1 for w in report['wakes'] if w['forced'])} wake(s) the driver had to finish."]
    lines += [f"- Turn {turn}, {f['kind']}: {f['summary']}" for turn, f in serious]
    lines += [f"- Turn {r['turn']}, the judge ({r['decision']}) on {report['seats'][r['seat']]}'s contest: {r['explanation']}" for r in rulings]
    if report.get("rubric"):
        lines += ["", rubric.render_markdown(report["rubric"]).rstrip()]
    usage = report["usage"]
    lines += ["", "## Usage", "", f"{usage['calls']} model calls, {usage['seconds']:.0f} seconds of model time, "
              f"{cost_summary(usage)}.",
              "", "`log.md` has the play-by-play, table talk included."]
    if report["stopped"]:
        lines += ["", f"**Stopped early:** {report['stopped']}"]
    return "\n".join(lines) + "\n"
