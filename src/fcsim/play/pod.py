"""A multiplayer game: one agent per seat, an auditor behind them, a directory of what happened.

The scheduler is simple on purpose. It wakes whichever seat the table is waiting for, passes
for a seat that has priority on someone else's turn with nothing on the stack, audits at every
checkpoint where something auditable happened, and turns the auditor's findings into waiting
triggers and correction wakes. Narrower watches, triage and disputes come later (PLAN.md §6, §8).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import random
import re
import secrets
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..agents import BudgetExceeded, Runner, RunnerError, cost_summary
from ..agents.runner import Reply, transient

from ..audit import SERIOUS, Auditor, Judge, describe
from ..audit.auditor import card_reference
from ..cards import CardDatabase, read_decklist, resolve, validate_deck
from ..mcp import McpServer
from ..rules import RulesReference
from ..table import Table, mechanics, scripts, triggers
from ..table.model import MECHANICS
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
- Every tool call is a round trip of a few seconds, so chain what you would do next anyway: cast, \
activate or add_trigger with "pass": true; resolve with "finish": true when those mutations are all \
that is left; and end your turn with one pass_priority carrying until "cleanup", your notes and your \
response policy.
- On your own turn, do not pass one step at a time: use pass_priority with `until`, and when you \
are finished use until "cleanup", which passes through the rest of your turn. The table passes \
for you through your own upkeep and draw steps when nothing is waiting there.
- Put your triggered abilities on the stack (add_trigger) and carry out state-based actions, except \
two the table does itself: a player at 0 life, 10 poison or 21 commander damage is removed, and a \
commander that goes to a graveyard or exile, or would go to a hand or library, goes to the command \
zone, from where it can be cast again. If you want your own commander to stay where it went (to \
reanimate it, or to recast it from exile for less), give "stay": true in the move that puts it there. When an \
effect sets up something for later ("return it at the beginning of the next end step"), record it \
with the `delay` mutation as you resolve it, and the table will bring it back to you at that step.

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
                          "policy": {"type": "string", "enum": ["ask", "ask_everything", "pass_unless_targeted", "pass_always"]},
                          "why": {"type": "string", "description": "One short sentence."}}}
_OPENING = {"type": "object", "additionalProperties": False, "required": ["keep", "bottom", "why"],
            "properties": {"keep": {"type": "boolean"}, "why": {"type": "string", "description": "One short sentence."},
                           "bottom": {"type": "array", "items": {"type": "string"},
                                      "description": "Handles of the cards to put on the bottom of your library, if you keep after more than one mulligan."}}}
_REPLY = {"type": "object", "additionalProperties": False, "required": ["reply"],
          "properties": {"reply": {"type": "string", "description": "What you say back, in a sentence or two; empty to stay silent."}}}
_REPLIES_PER_WAKE = 3
_TRIGGERS = {"type": "object", "additionalProperties": False, "required": ["triggers"], "properties": {"triggers": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["pending", "action", "because"],
    "properties": {"pending": {"type": "string", "description": "The waiting trigger's handle, such as t12."},
                   "action": {"type": "string", "enum": ["decline", "script", "wake"]},
                   "because": {"type": "string", "description": "One short sentence."},
                   "times": {"type": "integer", "description": "For a trigger that triggered several times: how many to put on the stack. The rest are declined."},
                   "script": {"type": "array", "items": {"type": "object"}}}}}}}
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
             shuffle_seats: bool = False, rewind_to: int | None = None, rewind_because: str = "",
             generic_guide: str | Path | None = None, background_audits: bool = True, echo=lambda line: print(line, flush=True)) -> dict:
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
    With `rewind_to`, a resumed game first returns to just after that event (the table's `rewind`: the
    abandoned line stays in the journal and nobody counts it), and whatever audit was owed there is made.
    `hook_library` is a JSON file of what auditors have written for each card in earlier games: a card
    in it has its triggers and holds from the moment it arrives, and the file learns from this game.
    With `background_audits` a turn's audit is asked while the next turn is played, when nothing that arrived needs
    describing first, and its verdict is applied at the next point where a player would have priority.
    `generic_guide` is a file every pilot reads before its own deck's primer: how to play Commander well, whatever
    the deck (decks/_generic/primer.md); the game keeps the text it began with.
    With `shuffle_seats` the decks are seated in an order drawn from the seed, so that who sits to whose
    left changes from game to game as well as who goes first; `runners[i]` still pilots `deck_paths[i]`."""
    from ..workshop.project import deck_argument  # the workshop reads games, so it is imported here and not at the top

    earlier = GameDirectory.open(resume) if resume else None
    if earlier:
        deck_paths = [earlier.path / "decks" / f"seat-{index}.txt" for index in range(len(earlier.read("config.json")["seats"]))]
        primers = [(p.read_text(encoding="utf-8") if (p := earlier.path / "decks" / f"seat-{index}.primer.md").is_file() else "")
                   for index in range(len(deck_paths))]  # a game keeps the guidance it began with, whatever the project has learned since
        projects = [None] * len(deck_paths)
        frozen = earlier.path / "decks" / "generic.primer.md"
        generic = frozen.read_text(encoding="utf-8") if frozen.is_file() else ""
    else:
        given = [deck_argument(path) for path in deck_paths]
        deck_paths, projects = [path for path, _ in given], [project for _, project in given]
        primers = [project.primer() if project else "" for project in projects]
        generic = Path(generic_guide).read_text(encoding="utf-8").strip() if generic_guide and Path(generic_guide).is_file() else ""
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
                     judge_runner=judge_runner, hook_library=hook_library, primers=primers, rewind_to=rewind_to, rewind_because=rewind_because,
                     generic=generic, background_audits=background_audits)
    seed = seed if seed is not None else secrets.randbelow(2 ** 31)
    if shuffle_seats:
        order = random.Random(f"seating-{seed}").sample(range(count), count)
        deck_paths, decks, runners = [deck_paths[i] for i in order], [decks[i] for i in order], [runners[i] for i in order]
        triage_runners = [triage_runners[i] for i in order] if triage_runners else None
        projects, primers = [projects[i] for i in order], [primers[i] for i in order]
        names = _names(decks)
    directory = GameDirectory(out_root, "pod_" + "-".join(names), seed)
    table = Table([{"name": name, "commanders": [c.name for c in deck.commanders], "library": [c.name for c in deck.library()]}
                   for name, deck in zip(names, decks, strict=True)], seed=seed,
                  gated=True if audit_every == "checkpoint" else "turn", on_event=directory.journal,
                  card_lookup=_lookup(database))
    for index, path in enumerate(deck_paths):
        directory.write(f"decks/seat-{index}.txt", read_decklist(path).to_mtgo())
        if primers[index]:
            directory.write(f"decks/seat-{index}.primer.md", primers[index])  # exactly what this pilot was told, frozen with the game
    if generic:
        directory.write("decks/generic.primer.md", generic)  # what every pilot was told about playing well, frozen with the game
    directory.pin_cards({card.name for deck in decks for card in [*deck.commanders, *(c for c, _ in deck.cards)]}, database)
    directory.pin_rules(rules)
    directory.write("config.json", {"mode": "pod", "decks": [str(p) for p in deck_paths], "seats": names, "cycles": cycles,
                                    "table": table.config, "models": [r.model for r in runners],
                                    "backends": [getattr(r, "backend", "custom") for r in runners],
                                    "auditor_model": auditor_runner.model,
                                    "auditor_backend": getattr(auditor_runner, "backend", "custom"),
                                    "projects": [project.hashes(database) if project else None for project in projects],
                                    "generic_hash": hashlib.sha256(generic.encode()).hexdigest()[:16] if generic else None,
                                    "deck_issues": [[i.to_dict() for i in validate_deck(d, database)] for d in decks]})
    return _play(directory, table, names, decks, runners, auditor_runner, database, cycles, rules, echo,
                 audit_every=audit_every, triage_runners=triage_runners, refresh_before_blocks=refresh_before_blocks,
                 judge_runner=judge_runner, hook_library=hook_library, primers=primers, generic=generic, background_audits=background_audits)


class _Pod:
    """One game being played: the driver's state, and everything it does between the table and the models.

    `run` schedules seats and audits until the game ends or `cycles` more rounds have been played."""

    def __init__(self, directory: GameDirectory, table: Table, names: list[str], decks: list, runners: list, auditor_runner,
                 database: CardDatabase, cycles: int, rules: RulesReference | None, echo, *, earlier: dict | None = None,
                 audit_every: str = "turn", triage_runners: list | None = None, refresh_before_blocks: bool = True, judge_runner=None,
                 hook_library: str | Path | None = None, ask_about_triggers: bool = True, primers: list[str] | None = None,
                 rewind_to: int | None = None, rewind_because: str = "", generic: str = "", background_audits: bool = True) -> None:
        self.background_audits = background_audits
        self.directory, self.table, self.names, self.decks, self.runners, self.auditor_runner, self.database, self.cycles, self.rules = \
            directory, table, names, decks, runners, auditor_runner, database, cycles, rules
        self.echo, self.earlier, self.audit_every, self.triage_runners, self.refresh_before_blocks, self.judge_runner, self.hook_library, self.ask_about_triggers = \
            echo, earlier, audit_every, triage_runners, refresh_before_blocks, judge_runner, hook_library, ask_about_triggers
        self.count, self.seed = len(self.names), self.table.config["seed"]
        self.table.on_event = self.directory.journal  # a replayed table was built without its journal
        if rewind_to is not None:  # a resumed game taken up again from an earlier event: journalled, like everything else
            self.table.rewind(rewind_to, rewind_because or "the game is taken up again from here")
        # A game's length is counted in rounds, each living player's own turns, so that eliminations do
        # not shorten it. On a resume the turn in progress (or just begun) is the first of the new rounds.
        game = self.table.game
        self.last_round = (game.seats[game.turn.active].turns_taken - 1 if self.earlier is not None else 0) + self.cycles
        self.context = f"A {self.count}-player Commander game. Seats, in turn order: " + ", ".join(f"{name} (seat {i})" for i, name in enumerate(self.names)) + "."
        self.auditor = Auditor(self.auditor_runner, self.database, context=self.context)
        self.judge = Judge(self.judge_runner or self.auditor_runner, self.database, rules=self.rules, context=self.context)
        self.primers = primers or [""] * len(names)
        self.generic = generic
        self.systems = [SYSTEM.format(name=name, count=self.count)
                        + (f"\n\nHOW TO PLAY COMMANDER WELL (what the pilots at this table have learned, whatever the deck. It is advice, not "
                           f"orders)\n{generic}" if generic else "")
                        + (f"\n\nHOW THIS DECK LIKES TO BE PLAYED (guidance accepted from its earlier games. It is advice, not orders: check the "
                           f"board and the cards as they are, and play to win)\n{primer}" if primer else "")
                        + self.rule_zero(index)
                        + "\n\nYOUR DECK (count, name, cost, type, text)\n" + _deck_reference(deck, self.database)
                        for index, (name, deck, primer) in enumerate(zip(self.names, self.decks, self.primers, strict=True))]
        self.current: dict = {}
        # Each seat's place in the log, so a wake can say what happened since its last one. After a
        # resume the players start from their notes and the board, not from the whole history again.
        self.seen = [len(self.table.events) if self.earlier is not None else 0] * self.count
        self.decisions: dict = {}  # who said they will respond to the stack as it currently stands
        self.waived: list[set] = [set() for _ in range(self.count)]  # per seat, the stack items it has said it will let resolve
        # What the auditor has written for each card so far, relative to its controller, so that another
        # copy of it, or the same card coming back, has its hooks from the moment it arrives and not from
        # the audit a turn later. In the second pod Wilhelt died, was recast, and missed his end-step trigger.
        self.library = Path(self.hook_library) if self.hook_library else None
        self.known_hooks: dict[str, list[dict]] = json.loads(self.library.read_text(encoding="utf-8")) if self.library and self.library.is_file() else {}
        self.hooked: set[int] = set()
        from_ledger: dict[str, list[dict]] = {}
        for entry in self.table.game.ledger:  # a resumed game already has a ledger to learn from, where the library knows nothing better
            source = self.table.game.objects.get(entry.get("source"))
            if source is not None:
                self.hooked.add(source.id)
                from_ledger.setdefault(source.name, []).append(_relative(entry, source.controller))
        self.known_hooks = {name: _tidy([_own_trigger(name, hook) for hook in hooks]) for name, hooks in {**from_ledger, **self.known_hooks}.items()}
        if self.earlier is not None and self.table.game.mechanics < MECHANICS:
            self.table.upgrade()  # from here on the game is played under the table's current rules; the journal says so
        if self.earlier is not None:
            # A resumed game may have been hooked from an older description of a card than the library now holds.
            for obj in [self.table.game.objects[i] for i in self.table.game.battlefield]:
                mine = [_relative(e, obj.controller) for e in self.table.game.ledger if e.get("source") == obj.id]
                if obj.name in self.known_hooks and mine and sorted(map(str, mine)) != sorted(map(str, self.known_hooks[obj.name])):
                    try:
                        self.table.rewrite_hooks(self.table.handles[None].of("c", obj.id), _absolute(self.known_hooks[obj.name], obj, self.database))
                    except ValueError:
                        pass
        self.cleared = -1  # how long the journal was when the turn so far was last audited because a player stood to lose
        self.nudged: dict[tuple[int, int], int] = {}  # (table turn, seat): times it was asked despite its policy, in someone's big turn
        self.overridden: set[int] = set()             # seats about to be asked only because of that
        self.pending: dict | None = None               # an audit being asked while the next turn is played
        self.background = None                         # the thread that asks it, while the game runs
        self.greeted: set[int] = set()  # permanents whose controller has had its chance to see to them on someone else's turn
        self.windows: dict = {}  # (table turn, step) -> the seats that said they would act in that empty-stack window
        self.stamped: list = [None]  # what the battlefield looked like when power and toughness were last worked out
        self.report: dict = {"directory": str(self.directory.path), "seed": self.seed, "seats": self.names, "stopped": None,
                        "rulings": list((self.earlier or {}).get("rulings", [])), "talk": list((self.earlier or {}).get("talk", [])),
                        "triage": list((self.earlier or {}).get("triage", [])), "refreshes": list((self.earlier or {}).get("refreshes", [])),
                        "wakes": list((self.earlier or {}).get("wakes", [])), "audits": list((self.earlier or {}).get("audits", [])),
                        "earlier_usage": [*(self.earlier or {}).get("earlier_usage", []), *([self.earlier["usage"]] if (self.earlier or {}).get("usage") else [])]}

    def holds_pen(self, seat: int) -> bool:
        return not self.table.awaiting_audit and seat in self.table.game.control.seats

    def tools_for(self, token: str):
        tools = self.current.get("tools")
        if tools is None or token != self.current.get("token"):
            return None

        def call(request_id: str, name: str, arguments: dict) -> dict:
            result = tools.call(request_id, name, arguments)
            me = self.current["seat"]
            if name == "say" and result.get("ok") and arguments.get("to") and self.current["replies"] < _REPLIES_PER_WAKE:
                self.current["replies"] += 1
                try:
                    result = {**result, "replies": self.table_talk(me, str(arguments.get("text", "")), arguments["to"])}
                except RunnerError:
                    pass  # talk is a courtesy: a failed question must not cost the speaker its turn
            if not self.holds_pen(me) and not self.current["ended"]:
                # The pen has left this seat. A new session costs seconds of start-up and a fresh read of
                # everything, so first do what needs no other player's full attention: if nobody responds
                # and the pen comes straight back, this wake simply carries on.
                try:
                    back = self.settle(me) == "yours"
                except RunnerError:
                    back = False  # a failed respond-or-pass question; the main loop will meet it again and report it
                if back:
                    control = self.table.game.control
                    why = control.detail.get("action", control.kind)
                    result = {**result, "pen": {"kind": why, "seats": control.seats},
                              "carry_on": f"Nobody else acted and the table is waiting for you again: {_WHY.get(why, why)}{_arithmetic(self.table, me)} "
                                          "Here is the table as it now stands.", "news": self.news(me), "table": self.briefing(me)}
                    self.seen[me] = len(self.table.events)
                else:
                    self.table.close_wake(self.current["wake"])
                    self.current["ended"] = True
            if isinstance(result, dict) and "pen" in result and self.current["ended"]:
                result["wake"] = "The table is no longer waiting for you. Stop calling tools and give your summary."
            return result
        return tools.definitions, call

    def when(self) -> str:
        game = self.table.game
        return (f"Round {game.seats[game.turn.active].turns_taken}, {self.names[game.turn.active]}'s turn "
                f"(table turn {game.turn.number}), {game.turn.step} step.")

    def hand_facts(self, seat: int) -> str:
        """What an opening hand holds, counted, and what players of the format keep. Two of four games of one deck began
        with seven cards, two lands and nothing cheap, kept though the first mulligan was free; one of them never saw a
        fourth land. The decision stays the pilot's, and its deck's primer may know better."""
        cards = [self.database.find(self.table.game.objects[i].name) for i in self.table.game.seats[seat].hand]
        known = [card for card in cards if card is not None]
        lands = [card for card in known if "Land" in card.table_facts()["types"]]
        spells = [card for card in known if card not in lands]
        rocks = [card.name for card in spells if card.table_facts().get("produces") and card.table_facts()["mana_value"] <= 3]
        cheap = [card.name for card in spells if card.table_facts()["mana_value"] <= 2]
        # Colours, which no pilot can count at the table: one kept four lands without white for a commander that costs {R}{W}{B},
        # sure that "the deck has plenty of white sources to draw into". It had 14 in 104 cards, and saw none in six turns.
        def makes(card) -> set[str]:
            facts = card.table_facts()
            return set(facts.get("produces") or []) & set("WUBRG") if "Land" in facts["types"] or (facts.get("produces") and facts["mana_value"] <= 3) else set()
        deck = self.decks[seat]
        wanted = [c for c in "WUBRG" if any(c in (commander.table_facts().get("mana_cost") or "") for commander in deck.commanders)]
        here = set().union(*(makes(card) for card in known)) if known else set()
        in_list = {c: sum(count for card, count in deck.cards if c in makes(card)) for c in wanted}
        size = sum(count for _, count in deck.cards) + len(deck.commanders)
        colours = (f" Your commander costs {' and '.join(c.table_facts().get('mana_cost') or '?' for c in deck.commanders)}. This hand's mana makes "
                   f"{', '.join(c for c in 'WUBRG' if c in here) or 'no colour'}" + (f" and not {', '.join(c for c in wanted if c not in here)}" if set(wanted) - here else "")
                   + f". In the whole list of {size}, sources of each: " + ", ".join(f"{c} {n}" for c, n in in_list.items()) + " (lands that fetch are not counted).") if wanted else ""
        return (f"Counted for you: {len(lands)} land(s), {len(rocks)} cheap mana source(s) besides ({', '.join(rocks) or 'none'}), "
                f"{len(cheap)} spell(s) costing two or less, out of {len(cards)} cards.{colours} As the format is usually played, a hand to keep has "
                "three or four lands, or two with cheap mana beside them, makes the colours of its first plays, and has something to do in "
                "the first three turns; the free mulligan is there to be used on a hand that does not.")

    def briefing(self, seat: int) -> str:
        """Everything a seat would otherwise spend two or three round trips fetching."""
        view = self.table.view(seat)
        mine = view["seats"][seat]["hand"].get("cards", [])
        in_play = [o.get("name") for o in view["battlefield"]] + [i["source"] for i in view["stack"]] \
            + [o.get("name") for s in view["seats"] for zone in ("command", "graveyard", "exile") for o in s[zone]]
        hand = []
        for card in mine:
            known = self.database.find(card["name"])
            face = known.front if known else None
            stats = f" {face.power}/{face.toughness}" if face and face.power is not None else ""
            hand.append(f"- [{card['handle']}] {card['name']}" + (f" {known.mana_cost} [{face.type_line}{stats}] "
                                                                   f"{' '.join(known.text.split())}" if known else ""))
        waiting = [f"- [{t['handle']}] {t['text']}" for t in view["waiting_triggers"] if t["seat"] == seat]
        asked = [f"- [{q['handle']}] from {self.names[q['from']]}: {q['prompt']}" + (f" Options: {q['options']}" if q["options"] else "")
                 for q in view["questions"] if q["waiting"] and q["waiting"][0] == seat]
        # A wake is a fresh session: without this a player repeats what it did a moment ago, such as
        # putting the same end-step trigger on the stack again each time it gets priority in that step.
        done = [f"- [{e['step']}] " + " ".join(e["public"]) for e in self.table.events
                if e["seat"] == seat and e["turn"] == self.table.game.turn.number and e["public"]
                and e["tool"] not in ("pass_priority", "write_notes")][-25:]
        return "\n".join(["WHAT YOU HAVE ALREADY DONE THIS TURN (do not repeat it: each ability triggers once per event)",
                          *(done or ["(nothing yet)"]), "",
                          "THE TABLE (the codes in brackets are your handles; NOW is a creature's current power and toughness as "
                          "the auditor last worked it out: trust it unless the board has changed since)",
                          describe(view, self.database), "", "YOUR HAND", *(hand or ["(empty)"]),
                          "", "WHAT YOUR UNTAPPED MANA CAN PAY FOR, by the table's count (one at a time; before cost reducers, extra costs, "
                          "Treasures or X), with a payment you can use as it stands:",
                          *([f"- {a['name']}: cast {{\"card\": \"{a['card']}\", \"payment\": "
                             + json.dumps({"tap": a["tap"], **({"pool": a["pool"]} if a.get("pool") else {})}) + "}"
                             for a in self.table.affordable(seat)] or ["- nothing"]),
                          *_before_attacking(self.table, seat),
                          *(["", "YOUR TRIGGERS WAITING TO GO ON THE STACK", *waiting] if waiting else []),
                          *(["", "QUESTIONS FOR YOU", *asked] if asked else []),
                          *(["", "THE JUDGE'S RULINGS IN THIS GAME (they bind everyone, the auditor included)",
                             *(f"- {r['text']}" for r in view["rulings"])] if view["rulings"] else []),
                          "", "CARDS IN PLAY", card_reference([n for n in in_play if n], self.database, rulings_per_card=0) or "(none)",
                          "", "YOUR NOTES", self.table.notes[seat] or "(none yet)"])

    def news(self, seat: int) -> str:
        events = self.table.act(seat, "get_log", {"since": self.seen[seat]})["events"]
        lines = [f"[{e['step']}] " + " ".join(e["text"]) + (" (to you: " + " ".join(e["private"]) + ")" if e.get("private") else "")
                 for e in events if (e["text"] and not e.get("passes") and (e["tool"] != "pass_priority" or len(e["text"]) > 1))
                 or e.get("private")][-40:]  # passes are noise, unless the table did something by itself as one was made
        return "Since your last wake:\n" + "\n".join(lines) if lines else "Nothing has happened since your last wake."

    def ask_all(self, prompts: dict[int, str], schema: dict, *, quick: bool = False, thorough: frozenset[int] | set[int] = frozenset()) -> list:
        """One question, no tools, to several seats at the same time: (seat, reply) for each. Every such
        question in a game comes through here. The caller builds the prompts, in this thread: the card
        database is not for several threads at once. `quick` uses the lower-effort runners, if there are any,
        except for the seats in `thorough`, whose answer matters enough to be thought through."""
        if not prompts:
            return []

        def runner(seat: int):
            return (self.triage_runners or self.runners)[seat] if quick and seat not in thorough else self.runners[seat]
        with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
            return list(pool.map(lambda seat: (seat, runner(seat).ask(self.systems[seat], prompts[seat], schema=schema)), prompts))

    def rule_zero(self, seat: int) -> str:
        """What the other players said their decks do before the game, as players do: each deck's own summary of its plan
        (the Gameplan section of its primer), or only its commander where it has none. A pilot who knows that Obeka takes
        extra upkeeps can see Obeka's big turn coming."""
        said = []
        for other, (name, deck, primer) in enumerate(zip(self.names, self.decks, self.primers, strict=True)):
            if other == seat:
                continue
            plan = re.search(r"^## Gameplan[^\n]*\n(.+?)(?=^## |\Z)", primer or "", re.S | re.M)
            commanders = " and ".join(c.name for c in deck.commanders)
            said.append(f"- {name} ({commanders}): " + (" ".join(plan[1].split()) if plan else "said nothing about its deck beyond its commander."))
        return "\n\nWHAT THE OTHER PLAYERS SAID THEIR DECKS DO (before the game, as players do; their cards are theirs to know)\n" + "\n".join(said) if said else ""

    def big_turn(self, seat: int) -> bool:
        """Whether to ask a seat whose standing policy says not to: another player has cast three or more spells this turn,
        and this seat holds an answer it could cast. At most twice in a turn. A policy set at the end of one's own turn
        cannot foresee a combo turn, and the answer held for "a real threat" goes unused through it."""
        game = self.table.game
        if game.turn.active == seat or game.turn.casts.get(game.turn.active, 0) < 3 or self.nudged.get((game.turn.number, seat), 0) >= 2:
            return False
        if not self.instants(seat):
            return False
        self.nudged[(game.turn.number, seat)] = self.nudged.get((game.turn.number, seat), 0) + 1
        self.overridden.add(seat)
        return True

    def instants(self, seat: int) -> list[str]:
        """The cards in hand this seat could cast right now at instant speed, by the table's count of its mana."""
        lookup = self.table.card_lookup or (lambda _: None)
        return [a["name"] for a in self.table.affordable(seat)
                if "Instant" in (lookup(a["name"]) or {}).get("types", []) or "Flash" in (lookup(a["name"]) or {}).get("keywords", [])]

    def triage(self, seats: list[int]) -> dict[int, bool]:
        """Ask several seats at once, each with one question and no tools, whether they want to
        respond to what is on the stack. Most answers are no, and a no costs seconds, not a wake.
        One answer can cover the whole stack, and the rest of the turn, so that a chain of
        triggers is one question and not one per trigger."""
        game = self.table.game
        top = game.stack[-1]
        # A seat that holds an answer it could cast is asked to think it through, not at the triage runner's low effort.
        # Miirym's pilot, holding Chaos Warp, said no ten times in a row during Obeka's 122-action turn, "holding it for a
        # real threat", while Obeka's extra upkeeps stole the Dragons that killed Miirym three turns later.
        held = {seat: self.instants(seat) for seat in seats}

        def weigh(seat: int) -> str:
            if not held[seat]:
                return ""
            active = game.turn.active
            despite = (" The table asks you despite your standing policy, because this is a big turn and you hold an answer."
                       if seat in self.overridden else "")
            return (f"\nYou could cast now: {', '.join(held[seat])}.{despite} It is {self.names[active]}'s turn, and they have cast "
                    f"{game.turn.casts.get(active, 0)} spell(s) in it so far. Before you pass, ask who is winning if this resolves and the "
                    "rest of this turn goes the way it is going. An answer saved \"for a real threat\" is wasted if the real threat is "
                    "this turn.")

        def question(seat: int) -> str:
            items = [f"  {n}. {self.names[item.controller]}'s {item.source_name}" + (f": {item.text}" if item.text else "")
                     + ("" if item.id in self.waived[seat] else "  (new since you were last asked)")
                     for n, item in enumerate(reversed(game.stack), 1)]
            return (f"{self.names[top.controller]}'s {top.source_name} is on top of the stack and you have priority. The stack, top first:\n"
                    + "\n".join(items) + "\nDo you want to respond now, before the top item resolves, by casting a spell or "
                    "activating an ability? Answer respond=false to pass; answer respond=true only if you would actually act "
                    "now, and you will then be woken to do it. If you pass, `ask_again` says what happens as this stack "
                    "resolves: false means you will let everything now on the stack resolve, and the table passes for you "
                    "until something new is added; true means you want to be asked again before each lower item resolves. "
                    "If you would respond to nothing at all before your next turn (you are tapped out, or hold nothing you "
                    "could use), also set `policy` to \"pass_always\", or to \"pass_unless_targeted\" to be asked only "
                    "about things that target you or what you control." + weigh(seat))
        prompts = {seat: f"{self.when()} {question(seat)}\n\n"
                         f"{self.news(seat)}\n\n{self.briefing(seat)}" for seat in seats}

        self.overridden -= set(seats)
        replies = self.ask_all(prompts, _TRIAGE, quick=True, thorough={seat for seat in seats if held[seat]})
        answers = {}
        for seat, reply in replies:
            data = reply.data or {}
            answers[seat] = bool(data.get("respond"))
            if not answers[seat]:  # asked again by default: a player who says nothing about the rest has only passed once
                self.waived[seat] = {top.id} if data.get("ask_again", True) else {item.id for item in game.stack}
                if data.get("policy") in ("ask_everything", "pass_unless_targeted", "pass_always"):
                    self.table.act(seat, "set_response_policy", {"policy": data["policy"]})
            record = {"turn": game.turn.number, "seat": seat, "respond": answers[seat], "why": data.get("why", ""),
                      "about": top.source_name, "stack": len(game.stack), "ask_again": bool(data.get("ask_again", True)),
                      "policy": data.get("policy"), "seconds": round(reply.seconds, 1), **({"holding": held[seat]} if held[seat] else {})}
            self.report["triage"].append(record)
            self.directory.append(f"transcript-seat-{seat}.jsonl", {"kind": "triage", **record})
        self.echo(f"turn {game.turn.number} {game.turn.step}: response to {top.source_name}: "
             + ", ".join(f"{self.names[s]} {'RESPONDS' if a else 'passes'}" for s, a in answers.items()))
        return answers

    def opening_hands(self, server: McpServer) -> None:
        """Every seat decides on its opening hand at the same time, with one question and no tools:
        the decisions are independent, and four sessions one after another took minutes before turn 1.
        A seat whose answer the table refuses gets an ordinary wake instead."""
        while self.table.game.control.kind == "setup":
            deciding = list(self.table.game.control.seats)

            def question(seat: int) -> str:
                taken = self.table.game.seats[seat].mulligans
                return (f"The game is about to begin. You are number {self.table.game.turn_order_from(self.table.game.turn.active).index(seat) + 1} of "
                        f"{self.count} in turn order. This is your opening hand, after {taken} mulligan(s). Keep it (keep=true) or mulligan "
                        "(keep=false): a mulligan shuffles it away and draws seven new cards. The first mulligan is free; for each one "
                        f"after that you put a card on the bottom when you keep. If you keep this hand you owe {max(0, taken - 1)}: list "
                        f"exactly that many handles in `bottom`. {self.hand_facts(seat)}\n\n{self.briefing(seat)}")
            prompts = {seat: question(seat) for seat in deciding}  # built here: the card database is not for several threads at once

            replies = self.ask_all(prompts, _OPENING)
            for seat, reply in replies:
                data, started = reply.data or {}, len(self.table.events)
                result = self.table.act(seat, "mulligan") if data.get("keep") is False else self.table.act(seat, "keep", {"bottom": data.get("bottom") or []})
                self.echo(f"turn 0 setup: {self.names[seat]} {'mulligans' if data.get('keep') is False else 'keeps'}: {data.get('why', '')}")
                entry = {"seat": seat, "turn": 0, "kind": "mulligan", "summary": data.get("why", ""), "events": [started, len(self.table.events)],
                         "tool_calls": 0, "tool_errors": [], "forced": None, "seconds": round(reply.seconds, 1),
                         "estimated_cost_usd": reply.estimated_cost_usd}
                self.report["wakes"].append(entry)
                self.directory.append(f"transcript-seat-{seat}.jsonl", {**entry, "calls": []})
                if "error" in result:
                    self.wake(server, seat, "mulligan")
                    if seat in self.table.game.control.seats and len(self.table.events) == started:
                        # Still undecided, and nothing it tried was accepted: keep for it, so that a game always starts.
                        hand = self.table.view(seat)["seats"][seat]["hand"]["cards"]
                        self.table.act(seat, "keep", {"bottom": [c["handle"] for c in hand[:max(0, self.table.game.seats[seat].mulligans - 1)]]})
                        self.report["wakes"][-1]["forced"] = "the driver kept the hand and chose what went on the bottom"
                self.seen[seat] = len(self.table.events)

    def table_talk(self, speaker: int, text: str, to: list) -> list[dict]:
        """A player has spoken to others by name. Each is asked at once, with no tools, what it says
        back, so that a deal can be struck before the speaker acts and not a round later."""
        game = self.table.game
        asked = [s for s in dict.fromkeys(to) if isinstance(s, int) and 0 <= s < self.count and s != speaker and game.seats[s].status == "alive"]
        prompts = {seat: (f"{self.when()} {self.names[speaker]} has just said to you, at the table: \"{text}\" You are not acting now, only "
                          "talking. Say what serves you: agree, refuse, bargain, bluff, point at the real threat. Deals are not "
                          f"binding. Leave `reply` empty to stay silent.\n\n{self.news(seat)}\n\n{self.briefing(seat)}") for seat in asked}

        answers = self.ask_all(prompts, _REPLY)
        said = []
        for seat, reply in answers:
            words = str((reply.data or {}).get("reply", "")).strip()
            record = {"turn": game.turn.number, "seat": seat, "to": speaker, "asked": text, "reply": words, "seconds": round(reply.seconds, 1)}
            self.report["talk"].append(record)
            self.directory.append(f"transcript-seat-{seat}.jsonl", {"kind": "reply", **record})
            if words and "error" not in self.table.act(seat, "say", {"text": words, "to": [speaker]}):
                said.append({"from": self.names[seat], "seat": seat, "text": words})
        self.echo(f"turn {game.turn.number} {game.turn.step}: {self.names[speaker]} spoke to {', '.join(self.names[s] for s in asked)}; "
             f"{len(said)} answered")
        return said

    def waiting_triggers(self, seat: int) -> bool:
        """The table is waiting for this seat's triggers and the seat has no session open. Most triggers need no
        session: they do not apply, or what they do can be said in the few operations the table carries out
        itself. One question, no tools: decline it, give its effect as a script, or ask to be woken. Returns
        False when a wake is needed after all (a target, a choice, an answer the table refused)."""
        view = self.table.view(seat)
        mine = [t for t in view["waiting_triggers"] if t["seat"] == seat]
        listed = "\n".join(f"- [{t['handle']}] {t['text']}" for t in mine)
        question = (f"{self.when()} The table is waiting for your triggered abilities:\n{listed}\nFor each, answer with its handle in `pending` and "
                    "one `action`. \"decline\": it does not really apply, or it is a \"may\" you do not want (say why in `because`). "
                    "\"script\": it applies, it has no target and leaves nothing to choose, and these operations say ALL it does; give them in "
                    "`script` and the table puts it on the stack and resolves it for you: counters {on, kind, delta}; life {who, delta}; "
                    "draw {who, count}; mill {who, count}; create_token {token: {name, types, power, toughness, text}, count}; move {what, "
                    "to}; tap {what}; untap {what}; delay {step, text, script}. `what` is \"self\" (the permanent the ability is on); `on` is "
                    "\"self\", \"each_creature_you_control\", \"each_other_creature_you_control\" or a player; a player (`who`) is \"you\", "
                    "\"each_opponent\", \"each_player\" or, for a trigger about one thing, \"that_player\" (who lost the life, was dealt the "
                    "damage, cast the spell); a `count` may be \"that_many\" and a `delta` \"that_many\" or \"minus_that_many\" for the amount "
                    "it was about; `to` is battlefield, graveyard, exile, hand or command. A script that turns out to do nothing is handed back "
                    "to you to resolve by hand. \"wake\": anything else "
                    "(a target, a choice, a card to look at, an effect these operations cannot say): you are woken to do it by hand. If "
                    "in doubt, \"wake\". For a trigger that says it triggered several times, `times` says how many go on the stack.")
        (_, reply), = self.ask_all({seat: f"{question}\n\n{self.news(seat)}\n\n{self.briefing(seat)}"}, _TRIGGERS, quick=True)
        answers = {a.get("pending"): a for a in (reply.data or {}).get("triggers", []) if isinstance(a, dict)}
        record = {"turn": self.table.game.turn.number, "seat": seat, "respond": False, "about": "its waiting triggers", "stack": len(self.table.game.stack),
                  "why": "; ".join(f"{a.get('action')}: {a.get('because', '')}" for a in answers.values())[:300], "seconds": round(reply.seconds, 1)}
        self.report["triage"].append(record)
        self.directory.append(f"transcript-seat-{seat}.jsonl", {"kind": "triage", **record, "answers": list(answers.values())})
        self.echo(f"turn {self.table.game.turn.number} {self.table.game.turn.step}: {self.names[seat]}'s triggers: "
             + (", ".join(str(a.get("action")) for a in answers.values()) or "no answer"))
        for trigger in mine:
            answer = answers.get(trigger["handle"], {})
            if answer.get("action") == "decline":
                result = self.table.act(seat, "skip_trigger", {"pending": trigger["handle"], "because": answer.get("because") or "declined"})
            elif answer.get("action") == "script" and answer.get("script"):
                result = {}
                for _ in range(max(1, min(int(answer.get("times") or 1), trigger.get("count", 1)))):
                    result = self.table.act(seat, "add_trigger", {"pending": trigger["handle"], "script": answer["script"]})
                    if "error" in result:
                        break
                still = next((t for t in self.table.view(seat)["waiting_triggers"] if t["handle"] == trigger["handle"]), None)
                if "error" not in result and still is not None and self.table.game.control.kind == "triggers":
                    result = self.table.act(seat, "skip_trigger", {"pending": trigger["handle"], "because": "the rest of them do nothing more"})
            else:
                return False
            if "error" in result:
                return False
        self.seen[seat] = len(self.table.events)
        return True

    def window(self, seats: list[int], situation: str) -> set[int]:
        """Ask several seats at once whether they would act now, with the stack empty on another player's
        turn. Without this nobody could ever kill an attacker, play a combat trick after blocks, or
        flash something in at the end of turn: the driver passes for everyone when nothing is on the stack."""
        game = self.table.game
        question = (f"It is {self.names[game.turn.active]}'s {game.turn.step} step, the stack is empty, and you have priority. {situation} Do you "
                    "want to act now, at instant speed: cast an instant or a card with flash, or activate an ability? Answer "
                    "act=false to pass. Answer act=true only if you would actually do it now; you will then be woken to do it.")
        prompts = {seat: f"{self.when()} {question}\n\n{self.news(seat)}\n\n{self.briefing(seat)}" for seat in seats}

        replies = self.ask_all(prompts, _WINDOW, quick=True)
        acting = set()
        for seat, reply in replies:
            data = reply.data or {}
            acting |= {seat} if data.get("act") else set()
            record = {"turn": game.turn.number, "seat": seat, "respond": bool(data.get("act")), "why": data.get("why", ""),
                      "about": f"{game.turn.step} window", "stack": 0, "seconds": round(reply.seconds, 1)}
            self.report["triage"].append(record)
            self.directory.append(f"transcript-seat-{seat}.jsonl", {"kind": "triage", **record})
        self.echo(f"turn {game.turn.number} {game.turn.step}: a chance to act: "
             + ", ".join(f"{self.names[s]} {'ACTS' if s in acting else 'passes'}" for s, _ in replies))
        return acting

    def wake(self, server: McpServer, seat: int, kind: str, extra: str = "") -> None:
        token, handle = secrets.token_hex(16), self.table.open_wake(seat, kind)
        self.current.update(tools=SeatTools(self.table, seat, handle, database=self.database, rules=self.rules), token=token, wake=handle,
                       seat=seat, ended=False, replies=0)
        game = self.table.game
        self.windows.get((game.turn.number, game.turn.step), set()).discard(seat)
        if kind == "correction" and not extra:
            extra = _correction(game.control.detail)
        prompt = f"{self.when()} {extra or _WHY.get(kind, 'The table is waiting for you.')}{_arithmetic(self.table, seat)}\n\n{self.news(seat)}\n\n{self.briefing(seat)}"
        self.echo(f"turn {game.turn.number} {game.turn.step}: {self.names[seat]} ({kind}) ...")
        started = len(self.table.events)
        failed = None
        for pause in (30, 90, 180, None):
            try:
                reply = self.runners[seat].ask(self.systems[seat], prompt, tools=server.connection(
                    token, tuple(t["name"] for t in self.current["tools"].definitions)))
                break
            except RunnerError as error:
                if not transient(str(error)) or pause is None:
                    raise
                if any(not c["error"] and c["tool"] not in ("get_view", "get_log", "get_notes", "lookup_card", "lookup_rule")
                       for c in self.current["tools"].calls):
                    # It acted before the session failed. Asked again with the same prompt it would act from a board that has
                    # moved; so the wake ends as it stands, and the table, still waiting for this seat, wakes it with a fresh briefing.
                    failed = str(error)
                    reply = Reply(data=None, text=f"(The session failed after {len(self.current['tools'].calls)} tool call(s): {failed[:160]})")
                    self.echo(f"         {self.names[seat]}'s session failed on the API's side after acting; it will be woken again")
                    break
                self.echo(f"         {self.names[seat]}'s session failed on the API's side before acting ({str(error)[:100]}); again in {pause} s")
                time.sleep(pause)
        self.table.close_wake(handle)
        forced = None
        if failed is None and (self.holds_pen(seat) and self.table.game.control.kind != "setup" or (kind == "mulligan" and self.holds_pen(seat))):
            forced = _default_action(self.table, seat)  # it stopped while the table still waited for it
        self.seen[seat] = len(self.table.events)
        entry = {"seat": seat, "turn": game.turn.number, "kind": kind, "summary": reply.text, "events": [started, len(self.table.events)],
                 "tool_calls": len(self.current["tools"].calls), "tool_errors": [c for c in self.current["tools"].calls if c["error"]],
                 "forced": forced, "seconds": round(reply.seconds, 1), "estimated_cost_usd": reply.estimated_cost_usd}
        self.report["wakes"].append(entry)
        self.directory.append(f"transcript-seat-{seat}.jsonl", {**entry, "calls": self.current["tools"].calls})
        self.directory.write(f"notes-seat-{seat}.md", self.table.notes[seat])
        self.current.update(tools=None, token=None, wake=None)

    def remember(self, hooks: dict[str, list[dict]]) -> None:
        """Write these cards' hooks into the library. Other games may be writing too: merge into what is on disk now, and swap the file in whole."""
        if not self.library or not hooks:
            return
        on_disk = json.loads(self.library.read_text(encoding="utf-8")) if self.library.is_file() else {}
        self.library.parent.mkdir(parents=True, exist_ok=True)
        scratch = self.library.with_suffix(f".{secrets.token_hex(4)}.tmp")
        scratch.write_text(json.dumps({**on_disk, **hooks}, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        scratch.replace(self.library)

    def owed(self) -> tuple | None:
        """After a resume: the snapshot the audit still owed would have begun from, or None if nothing is owed.
        A game can stop anywhere, and the second batch's second game stopped inside an audit, after the
        finding (Cyclonic Rift resolved against the wrong permanent) and before its correction. The resume
        then took its snapshot of the board as it found it, audited the nothing that had happened since,
        and released the turn. What is owed is everything that stands since the last release that stands."""
        events, gone = self.table.events, self.table.undone(self.table.events)
        since = max((e["n"] + 1 for e in events if e["tool"] == "release" and e["n"] not in gone),
                    default=next((e["n"] for e in events if e["turn"] >= 1), None))  # no audit yet: from the first turn's first event
        # A turn audited in the background was released before its verdict came. One whose verdict never came is owed too.
        checks = [json.loads(line) for line in (self.directory.path / "audits.jsonl").read_text(encoding="utf-8").splitlines() if line] \
            if (self.directory.path / "audits.jsonl").is_file() else []
        done = {c["events"][0] for c in checks if "kind" not in c and c.get("events")}
        unanswered = [c["events"][0] for c in checks if c.get("kind") == "deferred" and c["events"][0] not in done]
        if unanswered and since is not None:
            since = min(since, *unanswered)
        if since is None or not any(e["seat"] is not None and e["n"] not in gone for e in events[since:]):
            return None
        past = Table.replay(self.table.config, events[:since], card_lookup=self.table.card_lookup)
        return self.auditor.snapshot(past)

    def audit(self, server: McpServer, snapshot: tuple, *, losing: list[tuple[int, str]] | None = None, defer: bool = False) -> None:
        """`losing` are the seats the driver will remove once this audit is over, which is why it was called. The first
        game played with that audit had two findings that a player "has lost the game and is still listed as alive", and a
        correction opened for each that nobody could make: the auditor was looking at what the driver was about to do."""
        gone = self.table.undone(self.table.events)
        unit = [e for e in self.table.events[snapshot[2]:] if e["n"] not in gone]

        def needs_no_audit(event: dict) -> bool:
            """Nothing here for a rules check to find: talk and passing; and what the table itself has
            verified, where the card involved needs nothing written down for later. That is a land or a
            permanent spell the table accepted without a warning or a `because` (timing, zone and payment
            checked), whose card either does nothing but tap for mana or have keywords, or is already in
            the hook library with every trigger as a pattern; and combat declared without a `because` and
            dealt as the table worked it out."""
            tool, args = event["tool"], event["args"]
            if event["seat"] is None or not event["public"] or tool in ("pass_priority", "say", "write_notes", "keep", "mulligan"):
                return True
            if event["lints"] or args.get("because"):
                return False
            if tool in ("declare_attackers", "declare_blockers"):
                return True
            if tool == "combat_damage":
                return bool(args.get("as_proposed")) and not args.get("then")
            named = re.search(r" plays (.+) from .+'s hand(?: \(|\.)", event["public"][0]) if tool == "play_land" else \
                re.search(r" casts (.+?) from .+'s (?:hand|command)(?: targeting|,|\.| \[)", event["public"][0]) if tool == "cast" else None
            card = self.database.find(named[1]) if named else None
            if card is None or (tool == "cast" and (args.get("resolves") != "auto_permanent" or args.get("targets") or args.get("x") is not None
                                                    or (args.get("payment") or {}).get("note"))):
                return False
            return _needs_nothing_written(card, self.known_hooks)
        if all(needs_no_audit(e) for e in unit):
            return
        actions = sum(1 for e in unit if e["seat"] is not None and e["tool"] not in ("pass_priority", "say", "write_notes", "set_response_policy"))
        note = ("The table is about to remove " + "; ".join(f"{self.names[seat]} ({reason})" for seat, reason in losing) + " from the game as a state-based "
                "action. Nothing brings a player back, so the turn so far is checked first: say whether what led to it was played correctly. Do not "
                "report that they are still in the game; that is what happens next.") if losing else ""
        prompt, end, turn = self.auditor.prompt(self.table, snapshot, note=note), len(self.table.events), self.table.game.turn.number
        if defer and self.can_defer():
            # Between turns, and nothing arrived this turn that the next may need described first: the next turn starts at
            # once, and the verdict is applied when it comes. An audit was a fifth of a game, every second of it waited for.
            self.pending = {"future": self.background.submit(self.auditor.ask, prompt), "snapshot": snapshot, "unit": unit, "end": end, "turn": turn}
            self.directory.append("audits.jsonl", {"kind": "deferred", "turn": turn, "events": [snapshot[2], end]})
            self.echo(f"         auditing {actions} actions while play goes on ...")
            return
        self.echo(f"         auditing {actions} actions ...")
        verdict, reply = self.auditor.ask(prompt)
        self.apply_verdict(server, verdict, reply, snapshot=snapshot, unit=unit, end=end, turn=turn, losing=losing)

    def can_defer(self) -> bool:
        """Whether this turn's audit can run while the next one is played: every permanent that arrived this turn is one the
        auditor has nothing to write down for, so no trigger of the next turn waits on its description."""
        game = self.table.game
        if self.audit_every != "turn" or self.background is None:
            return False
        for obj in (game.objects[i] for i in game.battlefield):
            if obj.entered_turn != game.turn.number:
                continue
            if obj.face_down or (obj.token is not None and str(obj.token.get("text") or "").strip()):
                return False
            card = self.database.find(obj.name) if obj.token is None else None
            if obj.token is None and (card is None or not _needs_nothing_written(card, self.known_hooks)):
                return False
        return True

    def settle_audit(self, server: McpServer, *, wait: bool) -> bool:
        """Apply a deferred audit's verdict if it has come, or with `wait` once it comes. Only where the table can stop: at
        a checkpoint, where a player would have priority (the table is stopped there for it), or at the end of the game,
        where nothing can be corrected. Returns whether it was applied."""
        pending = self.pending
        if pending is None or (not wait and not pending["future"].done()):
            return False
        control = self.table.game.control.kind
        if control not in ("audit", "priority", "over"):
            if not wait:
                return False
            try:
                pending["future"].result()  # waited for, but applied only where it can be: the next checkpoint, or on resume
            except Exception:  # noqa: BLE001 - the marker stays in audits.jsonl, so a resume still owes it
                pass
            return False
        self.pending = None
        verdict, reply = pending["future"].result()  # a failed audit is raised here, where a failed audit always was
        stopped = control == "priority"
        if stopped:
            self.table.checkpoint()
        self.apply_verdict(server, verdict, reply, snapshot=pending["snapshot"], unit=pending["unit"], end=pending["end"], turn=pending["turn"],
                           late=True)
        if stopped and self.table.awaiting_audit:
            self.table.release()
        return True

    def apply_verdict(self, server: McpServer, verdict: dict, reply, *, snapshot: tuple, unit: list[dict], end: int, turn: int,
                      losing: list[tuple[int, str]] | None = None, late: bool = False) -> None:
        """What an audit found, put into effect: hooks, stamps, missed triggers, answered disputes and corrections."""
        if losing:  # whatever the note said: a finding that the player about to go has not gone yet is no finding
            leaving = {seat for seat, _ in losing}
            verdict["findings"] = [f for f in verdict.get("findings", []) if not (f.get("kind") == "sba" and f.get("seat") in leaving)]
            verdict["verdict"] = "findings" if any(f.get("kind") in SERIOUS for f in verdict["findings"]) else "ok"
        record = {"turn": turn, "events": [snapshot[2], end], **verdict, "seconds": round(reply.seconds, 1), **({"late": True} if late else {})}
        self.report["audits"].append(record)
        self.directory.append("audits.jsonl", record)
        if verdict.get("hooks"):
            counts: dict[tuple, int] = {}
            for hook in verdict["hooks"]:
                hook.pop("scripted", None)  # trust is the driver's to give, never the auditor's to claim
                source = self.table.game.objects.get(self.table.handles[None].resolve("c", hook.get("handle")) or -1)
                if source is None or not hook.get("script"):
                    continue
                known = next((h for h in self.known_hooks.get(source.name, []) if _hook_key(h) == _hook_key(hook)), {})
                times = (known.get("script_seen", 0) if known.get("script") == hook["script"] else 0) + 1
                counts[(source.name, _hook_key(hook))] = times
                hook["scripted"] = _script_trusted(self.database.find(source.name), {**hook, "script_seen": times, "script_revoked": known.get("script_revoked")})
            try:
                self.table.apply_hooks(verdict["hooks"])
            except ValueError as error:
                record["hooks_error"] = str(error)
            fresh: dict[str, list[dict]] = {}
            described = {self.table.handles[None].resolve("c", h.get("handle")) for h in verdict["hooks"]}
            for entry in self.table.game.ledger:  # the cards this audit described, as the ledger now holds them: one set each
                source = self.table.game.objects.get(entry.get("source"))
                if source is not None and source.id in described:
                    self.hooked.add(source.id)
                    learned = _own_trigger(source.name, _relative(entry, source.controller))
                    learned.pop("scripted", None)
                    if learned.get("script"):
                        before = next((h for h in self.known_hooks.get(source.name, []) if _hook_key(h) == _hook_key(learned)), {})
                        learned |= {"script_seen": counts.get((source.name, _hook_key(learned)), 1), "script_revoked": bool(before.get("script_revoked"))}
                    if learned not in fresh.setdefault(source.name, []):
                        fresh[source.name].append(learned)
            self.known_hooks.update(fresh)
            self.remember(fresh)
        for finding in verdict.get("findings", []):
            # One finding against a scripted resolution and the script is never trusted again, in this game or a later one.
            told = self.table.events[finding["event"]]["public"] if isinstance(finding.get("event"), int) and 0 <= finding["event"] < len(self.table.events) else []
            for line in told:
                # Only a finding that the resolution itself was wrong. A trigger somebody missed at the same event is not the
                # script's doing, and a script its player gave for one resolution is in no library: the second batch's second
                # game died here looking up "a triggered ability", the name a delayed trigger went by.
                blamed = line.split(" (")[0]
                if " resolves by script" in line and finding.get("kind") in ("illegal", "misresolved") and blamed in self.known_hooks:
                    for hook in self.known_hooks[blamed]:
                        hook["script_revoked"] = True
                    self.echo(f"         the script for {blamed} is revoked: {finding['summary'][:120]}")
                    for obj in [o for o in self.table.game.objects.values() if o.zone == "battlefield" and o.name == blamed]:
                        self.table.rewrite_hooks(self.table.handles[None].of("c", obj.id), _absolute(self.known_hooks[blamed], obj, self.database))
                    self.remember({blamed: self.known_hooks[blamed]})
        self.table.apply_computed(verdict.get("computed") or [])
        self.stamped[0] = _characteristics_print(self.table)
        actors = [e["seat"] for e in unit if e["seat"] is not None]
        at_fault: dict[int, list[str]] = {}
        for finding in verdict.get("findings", []):
            seat = finding.get("seat")
            seat = seat if isinstance(seat, int) and 0 <= seat < self.count and self.table.game.seats[seat].status == "alive" else None
            if finding["kind"] == "trigger":
                self.flag_missed(finding)
            if finding["kind"] == "trigger" and seat is not None:
                self.table.queue_trigger(seat, finding["summary"])
            elif finding["kind"] in SERIOUS and finding["kind"] != "trigger":
                seat = seat if seat is not None else (actors[-1] if actors else None)
                if seat is not None:
                    at_fault.setdefault(seat, []).append(f"{finding['summary']} Basis: {finding['basis']} What should happen: {finding['fix']}")
        if self.table.game.control.kind != "audit":
            return  # the game is over: nothing can be put right, and the findings stand in the record
        for answer in verdict.get("disputes") or []:
            raised = self.table.events[answer["event"]] if 0 <= answer.get("event", -1) < len(self.table.events) else None
            if raised is not None and raised["tool"] == "dispute":
                self.table.answer_dispute(raised["seat"], answer["upheld"], answer["answer"])
        for seat, problems in at_fault.items():
            self.table.open_correction(seat, " | ".join(problems))
            self.wake(server, seat, "correction")
            if self.table.game.control.kind == "dispute":
                self.adjudicate(snapshot[2])
                if self.table.game.control.kind == "correction":
                    self.wake(server, seat, "correction")  # the judge agreed there was a problem: no appeal this time
            if self.table.game.control.kind == "correction":
                self.table.act(seat, "finish_step")

    def last_audit(self, snapshot: tuple) -> None:
        """The stretch that ended the game, checked like any other. It never was: the loop stopped the moment somebody had
        won, so the one turn that decided the game went unread. In the second batch that was a 78-action turn that took three
        players from over thirty life each, with a dispute in it that nobody answered. Nothing can be corrected in a game
        that is over, so the report says whether the ending stands, and what is held against it."""
        gone = self.table.undone(self.table.events)
        unit = [e for e in self.table.events[snapshot[2]:] if e["n"] not in gone]
        if not any(e["seat"] is not None and e["tool"] not in ("pass_priority", "say", "write_notes", "set_response_policy") for e in unit):
            self.report["ending"] = "stands: everything up to the last loss had been audited before it was applied"
            return
        self.echo(f"         auditing the {len(unit)} events that ended the game ...")
        verdict, reply = self.auditor.audit(self.table, snapshot)
        record = {"turn": self.table.game.turn.number, "events": [snapshot[2], len(self.table.events)], **verdict, "seconds": round(reply.seconds, 1),
                  "final": True}
        self.report["audits"].append(record)
        self.directory.append("audits.jsonl", record)
        held = [f for f in verdict.get("findings", []) if f.get("kind") in SERIOUS]
        self.report["ending"] = ("contested: " + " | ".join(f["summary"] for f in held)) if held else "stands: the stretch that ended the game was audited"
        self.echo(f"         the ending {self.report['ending'][:200]}")

    def flag_missed(self, finding: dict) -> None:
        """A missed trigger of a permanent whose hook for it is text alone: say so on the ledger, so that the next
        audit is asked for a pattern in place of being told the card is done."""
        game = self.table.game
        for obj in [game.objects[i] for i in game.battlefield]:
            mine = [e for e in game.ledger if e.get("source") == obj.id]
            bare = [e for e in mine if e.get("kind") == "event_trigger" and not e.get("on") and not e.get("missed")]
            if bare and obj.name.split(",")[0] in str(finding.get("summary", "")):
                hooks = [{**{k: v for k, v in e.items() if k != "source"}, **({"missed": True} if e in bare else {})} for e in mine]
                self.table.rewrite_hooks(self.table.handles[None].of("c", obj.id), hooks)

    def adjudicate(self, since: int) -> None:
        """A player has contested a finding. In the first real game nobody could, and two players gave
        up a card each to a false alarm about the free mulligan."""
        detail = dict(self.table.game.control.detail)
        self.echo(f"         {self.names[detail['seat']]} contests the finding; the judge is ruling ...")
        decision, reply = self.judge.rule(self.table, since)
        self.table.rule(decision["decision"], decision["explanation"], ruling=decision.get("ruling", ""), fix=decision.get("fix", ""))
        record = {"kind": "ruling", "turn": self.table.game.turn.number, "seat": detail["seat"], "finding": detail["finding"],
                  "objection": detail["objection"], **decision, "seconds": round(reply.seconds, 1)}
        self.report["rulings"].append(record)
        self.directory.append("audits.jsonl", record)
        self.echo(f"         the judge: {decision['decision']}. {decision['explanation']}")

    def refresh(self) -> None:
        """Work the creatures' power and toughness out again. Audits are a turn apart, and blocks are
        chosen on these numbers: in the first real game a 4/4 was blocked and dealt damage as a 3/3."""
        self.echo("         working out power and toughness before blocks ...")
        computed, reply = self.auditor.characteristics(self.table)
        self.table.apply_computed(computed)
        self.stamped[0] = _characteristics_print(self.table)
        record = {"turn": self.table.game.turn.number, "kind": "characteristics", "computed": computed, "seconds": round(reply.seconds, 1)}
        self.report["refreshes"].append(record)
        self.directory.append("audits.jsonl", record)

    def rehook(self) -> None:
        """Give a permanent that has just arrived the hooks the auditor wrote for that card before."""
        game = self.table.game
        for obj in [game.objects[i] for i in game.battlefield if i not in self.hooked]:
            self.hooked.add(obj.id)
            if obj.face_down or obj.name not in self.known_hooks:
                continue
            hooks = [{**hook, "handle": self.table.handles[None].of("c", obj.id)} for hook in _absolute(self.known_hooks[obj.name], obj, self.database)]
            try:
                self.table.apply_hooks(hooks)
            except ValueError:
                continue  # the next audit writes them afresh
            # It has already entered, and the table matched that event before these hooks existed: its own
            # "when this enters" is put in front of its controller now, unless the player has seen to it.
            seen_to = any(e["tool"] == "add_trigger" and e["args"].get("source") == obj.id for e in self.table.events[-40:])
            for hook in hooks:
                on = hook.get("on") or {}
                if hook.get("kind") == "event_trigger" and on.get("event") == "enters" and on.get("self") and not seen_to \
                        and obj.entered_turn == game.turn.number:
                    self.table.queue_trigger(obj.controller, f"{hook.get('text', '')} (because {obj.name} entered)")

    def settle(self, me: int | None = None) -> str:
        """Do everything that needs no full wake: release mid-turn checkpoints, pass for seats with
        nothing to respond to, ask the rest whether they respond. Returns why it stopped: "over",
        "stuck", "audit" (the table waits for one), "wake" (some seat must decide), or "yours" when
        called from inside seat `me`'s wake and the pen has come back to it."""
        while True:
            self.rehook()
            game = self.table.game
            if game.control.kind == "over" or game.seats[game.turn.active].turns_taken > self.last_round:
                return "over"
            lost = _must_lose(self.table)
            if lost and me is not None:
                return "wake"  # end this wake first: correcting the table kills open wakes
            if lost and self.audit_every == "turn" and game.control.kind == "priority" and self.cleared != len(self.table.events):
                return "loss"  # nothing brings a player back: what led here is audited, and corrected if need be, first
            if lost:
                # Losing the game is a state-based action the table can see for itself, and nobody
                # should receive priority first (rule 704.3). In the first real game two players sat
                # at -1 life while priority went round.
                self.table.patch([{"op": "eliminate", "seat": seat, "reason": reason} for seat, reason in lost],
                            "state-based action: " + "; ".join(f"{self.names[seat]} loses ({reason})" for seat, reason in lost))
                continue
            if self.table.awaiting_audit:
                # By default a turn is audited whole, before the next one begins: that is when its
                # hooks must exist. Checkpoints inside the turn are released at once.
                if self.audit_every == "checkpoint" or game.control.detail["resume"]["do"] == "next_turn":
                    return "audit"
                self.table.release()
                continue
            if game.control.kind == "dispute":
                return "dispute"  # only after a resume: a ruling normally follows the contest at once
            expected = self.table.expected()
            if not expected:
                return "stuck"
            seat, kind = expected[0]
            quiet_step = seat == game.turn.active and game.turn.step in ("upkeep", "draw")
            if kind == "priority" and not game.stack and seat != game.turn.active:
                # An empty stack on someone else's turn. Usually there is nothing to do and the driver
                # passes; at the moments that matter the seats that could act are asked first, together.
                key = (game.turn.number, game.turn.step)
                if key not in self.windows:
                    waiting = [s for s in game.turn_order_from(seat) if s not in game.turn.passed]
                    situation = {s: _window(self.table, s) for s in waiting}
                    asked = [s for s in waiting if situation[s] and s != me]
                    self.windows.clear()
                    self.windows[key] = (self.window(asked, next(situation[s] for s in asked)) if asked else set()) | ({me} if situation.get(me) else set())
                if seat in self.windows[key]:
                    if seat != me:
                        return "wake"  # its wake takes it off the list: if it passes after all, the window is over for it
                    self.windows[key].discard(seat)
                    return "yours"
            if kind == "priority" and not game.stack and seat != game.turn.active:
                # A permanent of theirs has arrived on somebody else's turn (a flash creature, a token maker): the driver
                # is about to pass for them, and its "when this enters" would wait for the audit. In the fifth pod
                # Aang, Swift Savior came in during an attack and its controller never got to stack the trigger.
                arrived = [o for o in (game.objects[i] for i in game.battlefield) if o.controller == seat and o.id not in self.greeted
                           and o.entered_turn == game.turn.number and o.token is None
                           and not any(e.get("source") == o.id for e in game.ledger)
                           and not ((card := self.database.find(o.name)) is not None and _needs_nothing_written(card, self.known_hooks))]
                self.greeted.update(o.id for o in game.objects.values() if o.controller == seat and o.zone == "battlefield")
                if arrived:
                    if seat != me:
                        return "wake"
                    return "yours"
            if kind == "priority" and not game.stack and (quiet_step or seat != game.turn.active):
                # Nothing to respond to on someone else's turn; and nothing waiting in one's own
                # upkeep or draw step, or the table would be asking for a trigger, not priority. That
                # holds for the seat whose wake this is too: in the second real game the last player
                # to keep was told it had priority, in its upkeep, and played a land there.
                self.table.act(seat, "pass_priority")
                continue
            if seat == me:
                return "yours"
            if kind in ("priority", "declare_blockers") and _cannot_act(self.table, seat, kind):
                self.table.act(seat, _DEFAULTS[kind])  # an empty hand and nothing untapped; or nothing that could block
                continue
            if kind == "triggers" and seat != me and self.ask_about_triggers and self.waiting_triggers(seat):
                continue
            if kind == "combat_damage" and seat != me and _plain_combat(self.table):
                # Nothing has happened since the attack that could change how damage is dealt, and the sums
                # are the table's: no session is opened for the attacker to say yes to them.
                if "error" not in self.table.act(seat, "combat_damage", {"as_proposed": True}):
                    continue
            if kind == "declare_blockers" and self.refresh_before_blocks and self.stamped[0] != _characteristics_print(self.table):
                if me is not None:
                    return "wake"  # the attacker's wake is over either way: somebody has to choose blocks
                self.refresh()
                continue
            if kind == "priority" and game.stack:
                # A chance to respond. Ask everyone still to act at once, except those who have
                # already said they will let the top item resolve, or have a standing "pass for me".
                signature, top = tuple(item.id for item in game.stack), game.stack[-1].id
                if self.decisions.get("stack") != signature:
                    self.decisions.clear()
                    self.decisions.update(stack=signature, respond=set())
                waiting = [s for s in game.turn_order_from(seat) if s not in game.turn.passed and s != me]
                asked = [s for s in waiting if s not in self.decisions["respond"] and top not in self.waived[s]
                         and (_wants_asking(self.table, s) or self.big_turn(s))]
                if asked:
                    self.decisions["respond"] |= {s for s, responds in self.triage(asked).items() if responds}
                if seat not in self.decisions["respond"]:
                    self.table.act(seat, "pass_priority")
                    self.seen[seat] = len(self.table.events)
                    continue
            return "wake"

    def run(self) -> dict:
        started = time.monotonic()
        self.background = ThreadPoolExecutor(max_workers=1) if self.audit_every == "turn" and self.background_audits else None
        with McpServer(self.tools_for) as server:
            try:
                self.opening_hands(server)
                snapshot = (self.owed() if self.earlier is not None else None) or self.auditor.snapshot(self.table)
                while True:
                    why = self.settle()
                    if why in ("over", "stuck"):
                        self.settle_audit(server, wait=True)
                        if self.table.game.control.kind == "over":
                            self.last_audit(snapshot)
                        break
                    if why == "dispute":
                        self.adjudicate(snapshot[2])
                        continue
                    if why in ("audit", "loss"):
                        if why == "loss":
                            # A player stands to be removed. In the second batch Zaxara went from 9 life to 0 by combat damage the
                            # table's own bug dealt twice; the audit a turn later had the damage undone, and no tool could bring
                            # the player back. So the turn so far is audited before anybody goes, while it can still be put right.
                            self.table.checkpoint()
                        self.settle_audit(server, wait=True)  # the turn before is judged first, and put right where it must be
                        self.audit(server, snapshot, losing=_must_lose(self.table) if why == "loss" else None, defer=why == "audit")
                        self.table.release()
                        snapshot = self.auditor.snapshot(self.table)
                        self.cleared = len(self.table.events)  # if the loss still stands with nothing new to read, it is applied
                        self.directory.write("state.json", self.table.game.to_dict())
                        continue
                    if self.settle_audit(server, wait=False):
                        continue  # a deferred verdict came and was put into effect; the table may be waiting on someone else now
                    seat, kind = self.table.expected()[0]
                    before = len(self.table.events)
                    self.wake(server, seat, kind)
                    if len(self.table.events) == before and self.table.expected() and self.table.expected()[0] == (seat, kind):
                        self.report["stopped"] = f"{self.names[seat]} could not act ({kind})"  # never spin
                        break
            except BudgetExceeded as stop:
                self.report["stopped"] = str(stop)
            except RunnerError as error:
                self.report["stopped"] = f"model error: {error}"
            except Exception as error:  # noqa: BLE001 - a bug in the driver or the table must not cost the game
                # The journal holds every accepted call, so the game can be resumed once the bug is fixed; say what happened
                # where it will be seen. The second game of the first batch died of an attack on a planeswalker and left no report.
                self.report["stopped"] = f"internal error: {error!r}"
                self.directory.write("error.txt", traceback.format_exc())
                self.echo(f"INTERNAL ERROR, the game is stopped and can be resumed: {error!r}")
        if self.background is not None:
            self.background.shutdown(wait=True)  # an audit still out is waited for; not applied, it stays owed (see `owed`)
        game = self.table.game
        self.directory.flush_log()
        self.report["standings"] = [{"seat": s.index, "name": s.name, "status": s.status, "life": s.life, "hand": len(s.hand),
                                "permanents": sum(1 for i in game.battlefield if game.objects[i].controller == s.index),
                                "commander_damage": s.commander_damage, "commander_casts": s.commander_casts} for s in game.seats]
        self.report["winner"] = self.names[game.winner] if game.winner is not None else None
        self.report["turns_played"] = game.turn.number
        capped = game.control.kind != "over" and game.seats[game.turn.active].turns_taken > self.last_round  # begun, and stopped there
        self.report["rounds"] = max(s.turns_taken - (1 if capped and s.index == game.turn.active else 0) for s in game.seats)
        # On the clock, as against the models' own seconds: what a batch of games actually waits for. A resumed game adds its runs.
        self.report["wall_seconds"] = round((self.earlier or {}).get("wall_seconds", 0) + time.monotonic() - started)
        self.report["usage"] = self.auditor_runner.budget.to_dict()
        try:  # the journal is the game: say at once if it no longer rebuilds what was played, not when a later tool trips on it
            rebuilt = Table.replay(self.table.config, self.directory.events(), card_lookup=self.table.card_lookup)
            self.report["journal"] = "replays to the same game" if rebuilt.game.to_dict() == game.to_dict() else "REPLAYS TO A DIFFERENT GAME"
        except Exception as error:  # noqa: BLE001 - whatever stops a replay is the finding
            self.report["journal"] = f"DOES NOT REPLAY: {error}"
        self.directory.write("state.json", game.to_dict())
        self.directory.write("usage.json", self.report["usage"])
        self.directory.write("report.json", self.report)
        self.directory.write("report.md", render(self.report))
        if self.count > 1:  # after the report is safe on disk: the scores need a replay of the whole journal
            self.report["rubric"] = rubric.measure(self.directory.path)
            self.directory.write("rubric.json", self.report["rubric"])
            self.directory.write("report.json", self.report)
            self.directory.write("report.md", render(self.report))
        return self.report




def _play(*args, **kwargs) -> dict:
    """Schedule seats and audits until the game ends or `cycles` more rounds have been played."""
    return _Pod(*args, **kwargs).run()


def _before_attacking(table: Table, seat: int) -> list[str]:
    """On a seat's own turn, before it declares attackers: what the others could swing back with, and what it would have
    left to block. The table can count this much from printed values, counters and the auditor's numbers, and a pilot
    left to count it did not: one attacked with everything two turns running and went from 40 life to out in two rounds."""
    game, lookup = table.game, table.card_lookup
    if game.turn.active != seat or game.turn.step not in ("main1", "begin_combat", "declare_attackers") or game.turn.attackers:
        return []

    def creatures(index: int) -> list:
        return [game.objects[i] for i in game.battlefield
                if game.objects[i].controller == index and "creature" in game.objects[i].type_line.lower() and not game.objects[i].phased_out]
    lines = ["", "BEFORE YOU ATTACK (the table's count, from printed values, counters and the auditor's numbers; what cards do to combat is "
                 "not counted). Each opponent untaps before it can attack you:"]
    for other in game.seats:
        if other.index == seat or other.status != "alive":
            continue
        known = [mechanics.stats(game, obj, lookup) for obj in creatures(other.index)]
        power, flying = sum(k["power"] for k in known if k), sum(k["power"] for k in known if k and "flying" in k["keywords"])
        unsure = sum(1 for k in known if k is None)
        lines.append(f"- {other.name}: {other.life} life; {len(known)} creature(s) with {power} power" + (f", {flying} of it flying" if flying else "")
                     + (f", and {unsure} whose power the table does not know" if unsure else ""))
    mine = [(obj, mechanics.stats(game, obj, lookup)) for obj in creatures(seat)]
    stays = [obj.name for obj, k in mine if k and "vigilance" in k["keywords"]]
    air = [obj.name for obj, k in mine if k and k["keywords"] & {"flying", "reach"}]
    lines.append(f"- You: {game.seats[seat].life} life; {len(mine)} creature(s). Whatever attacks now without vigilance stays tapped through "
                 "every other player's turn and cannot block" + (f"; vigilance: {', '.join(stays)}" if stays else "")
                 + (f"; can block fliers: {', '.join(air)}" if air else "; nothing of yours can block a flier") + ".")
    return lines


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


def _needs_nothing_written(card, known_hooks: dict) -> bool:
    """A card the auditor has nothing to write down for: it only taps for mana or has keywords the
    table is already told, or every hook an auditor wrote for it before is one the table handles."""
    if any(h.get("script") and h.get("script_seen", 0) < 2 and not h.get("script_revoked") for h in known_hooks.get(card.name, [])):
        return False  # its script is waiting for a second opinion, which only an audit can give
    if card.name in known_hooks:
        return all(h.get("kind") in ("step_trigger", "hold") or (h.get("kind") == "event_trigger" and (h.get("on") or {}).get("event"))
                   for h in known_hooks[card.name])
    keywords = {k.lower() for k in card.keywords}
    for line in (re.sub(r"\([^)]*\)", "", line).strip() for face in card.faces for line in face.oracle_text.splitlines()):
        if not line or re.fullmatch(r"\{T\}: Add [^.]*\.", line):
            continue
        if not all(part.strip().lower() in keywords for part in re.split(r"[,;]", line)):
            return False
    return True


_CHOICES = re.compile(r"\b(may|targets?|chooses?|up to|unless|if|random|any number|instead|x|that many|for each|equal to|where|or|each time|twice)\b", re.I)


def _script_trusted(card, hook: dict) -> bool:
    """Whether the table may carry this hook's script out by itself. All of: the script uses only the
    table's own operations; the auditor has written the same script for this card at two audits or more;
    no finding has ever been made against it; and the card's printed ability, found from the hook's
    text, has no choice, target, condition or variable amount in its effect."""
    if card is None or not hook.get("script") or scripts.problem(hook["script"]) or hook.get("script_revoked") or hook.get("script_seen", 0) < 2:
        return False
    lines = [re.sub(r"\([^)]*\)", "", line).strip() for face in card.faces for line in face.oracle_text.splitlines()]
    scored = [(difflib.SequenceMatcher(None, str(hook.get("text", "")).lower(), line.lower()).ratio(), line) for line in lines if line]
    ratio, printed = max(scored, default=(0, ""))
    effect = printed.split(", ", 1)[1] if ", " in printed else printed
    if any(step.get(k) in scripts.AMOUNTS for step in hook["script"] if isinstance(step, dict) for k in ("count", "delta")):
        effect = re.sub(r"\bthat (many|much)\b", "", effect, flags=re.I)  # a variable amount the table saw for itself is no unknown
    return ratio >= 0.55 and not _CHOICES.search(effect)


def _absolute(hooks: list[dict], obj, database: CardDatabase) -> list[dict]:
    """Library hooks made ready for one permanent: its controller put back where the library says "its
    controller", and each script marked trusted or not."""
    ready = []
    for hook in hooks:
        made = {k: (obj.controller if v == "its controller" else v) for k, v in hook.items() if k not in ("script_seen", "script_revoked", "scripted")}
        if hook.get("script"):
            made["scripted"] = _script_trusted(database.find(obj.name), hook)
        ready.append(made)
    return ready


def _hook_key(hook: dict) -> tuple | None:
    """What makes two hooks the same ability: their kind and what they fire on, not their wording."""
    on = hook.get("on") or {}
    if not on and hook.get("kind") == "event_trigger":
        return None
    return (hook.get("kind"), hook.get("step"), str(hook.get("whose", hook.get("seat"))),
            *(str(triggers.pattern(on).get(k)) for k in ("event", "from", "to", "who", "self", "attached", "other", "kind", "sign")), len(on.get("any_of") or []))


def _tidy(hooks: list[dict]) -> list[dict]:
    """One hook for one ability. Descriptions of the same card written at different times differ in
    wording and in detail; of those that would fire on the same thing, the first stands. A hook that is
    text alone is dropped when a patterned one says much the same."""
    kept: list[dict] = []
    for hook in hooks:
        key = _hook_key(hook)
        if key is not None and hook.get("kind") == "static":
            key = (*key, hook.get("text"))
        if key is None or not any(key == k for k, _ in kept):
            kept.append((key, hook))
    patterned = [h for k, h in kept if k is not None and h.get("kind") == "event_trigger"]
    return [h for k, h in kept if k is not None or not any(
        difflib.SequenceMatcher(None, str(h.get("text", "")).lower(), str(p.get("text", "")).lower()).ratio() > 0.5 for p in patterned)]


def _own_trigger(name: str, hook: dict) -> dict:
    """A backstop for a hook whose text plainly says it triggers on its own permanent ("Whenever The
    Wise Mothman enters or attacks", "When this land enters") but whose `on` does not: without
    `self` the table would offer it whenever anything entered or attacked."""
    if hook.get("kind") != "event_trigger":
        return hook
    if re.search(r"\bwhen(?:ever)? (?:equipped|enchanted|fortified) (?:creature|land|permanent|artifact)\b", str(hook.get("text", "")), re.I):
        # The same for what it is attached to: written as "anyone's creature attacks", Pip-Boy 3000 asked
        # its controller about every attack at the table while it lay unattached.
        on = {k: v for k, v in (hook.get("on") or {}).items() if k != "who"}
        return {**hook, "on": {**on, "attached": True}} if on.get("event") or on.get("any_of") else hook
    short = re.escape(name.split(",")[0])
    found = re.search(rf"\bwhen(?:ever)? (?:this \w+|{re.escape(name)}|{short}) (enters|attacks|dies|leaves)", str(hook.get("text", "")), re.I)
    if not found:
        return hook
    on = dict(hook.get("on") or {})
    on.setdefault("event", found[1].lower())
    on.setdefault("who", "you")
    return {**hook, "on": {**on, "self": True}}


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
    policy, top = game.seats[seat].response_policy, game.stack[-1]
    at_me = any(t.get("seat") == seat or (t.get("obj") in game.objects and game.objects[t["obj"]].controller == seat) for t in top.targets)
    if policy == "pass_unless_targeted" or (policy == "ask" and top.kind == "triggered"):
        # A triggered ability that points at nothing of theirs: in the fifth pod that was most of 107 questions,
        # and nobody answered one. A player who holds a Stifle says so with the policy "ask_everything".
        return at_me
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
              f"The journal {report.get('journal', 'was not checked')}. {len(report['audits'])} audits; {len(serious)} finding(s); "
              + (f"{len(rulings)} contested ({', '.join(f'{n} {d}' for d in ('uphold', 'overrule', 'amend') if (n := sum(1 for r in rulings if r['decision'] == d)))}); " if rulings else "") +
              f"{sum(len(w['tool_errors']) for w in report['wakes'])} rejected tool call(s); "
              f"{sum(1 for w in report['wakes'] if w['forced'])} wake(s) the driver had to finish."]
    lines += [f"- Turn {turn}, {f['kind']}: {f['summary']}" for turn, f in serious]
    if report.get("ending"):
        lines += ["", f"**The ending {report['ending']}**" + (" Nothing can be corrected in a game that is over: read the result with that in mind."
                                                           if report["ending"].startswith("contested") else "")]
    lines += [f"- Turn {r['turn']}, the judge ({r['decision']}) on {report['seats'][r['seat']]}'s contest: {r['explanation']}" for r in rulings]
    if report.get("rubric"):
        lines += ["", rubric.render_markdown(report["rubric"]).rstrip()]
    usage = report["usage"]
    lines += ["", "## Usage", "", (f"{report['wall_seconds'] / 60:.0f} minutes on the clock ({report['wall_seconds'] / max(report['turns_played'], 1):.0f} s a "
                                     "table turn); " if report.get("wall_seconds") else "") + f"{usage['calls']} model calls, {usage['seconds']:.0f} seconds of model time, "
              f"{cost_summary(usage)}.",
              "", "`log.md` has the play-by-play, table talk included."]
    if report["stopped"]:
        lines += ["", f"**Stopped early:** {report['stopped']}"]
    return "\n".join(lines) + "\n"
