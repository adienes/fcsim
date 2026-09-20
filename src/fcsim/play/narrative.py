"""Write the story of a finished or unfinished game from its directory (PLAN.md §11.1).

Unlike `log.md`, which is the table as the players saw it, the narrative is written with full
information: every hand and draw, each player's own account of each wake, and the auditor's
findings. Rounds are condensed one at a time, then the story is written from those, so a long
game is never narrated in one pass. Everything goes in one file, `narrative.md`: the story, then
the round accounts it was written from, which are reused when the story is rewritten.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..cards import CardDatabase
from ..table import Table
from .gamedir import GameDirectory
from .pod import _lookup

STYLES = {
    "recap": "A clear recap a player could skim in two minutes: plain, chronological, no flourish.",
    "story": "A readable story of the game with some colour, the way a player retells a good night at the table. Never invent events.",
    "analysis": "An analyst's report: terse on what happened, thorough on why, on the decisions that mattered and on the decks.",
}

ROUND_SYSTEM = """\
You are keeping the record of a multiplayer game of Magic: The Gathering (Commander) played by \
AI agents, for the person who built one of the decks. You have full information: the table log, \
cards drawn and looked at in private, each player's own account of what it did and why, and the \
rules auditor's findings. Condense ONE ROUND into a faithful account of 150 to 300 words in plain \
prose: what each player did on their turn and why, anything said at the table, anything the \
auditor corrected, and anything that with hindsight was a mistake or a turning point. Name cards \
exactly. Do not invent anything that is not in the record, and do not pad a quiet round. Write \
paragraphs only, with no headings."""

STORY_SYSTEM = """\
You are writing up a multiplayer game of Magic: The Gathering (Commander) played by AI agents, for \
the person who built one of the decks. You are given the pod, the result, and a faithful account \
of each round written with full information. Write the narrative in Markdown with these sections:

# <a short title for this game>
## The pod and the result
## How it went          (round by round; this is the body)
## Turning points
## Table politics       (deals, accusations, who was believed; say so if there was little)
## Mistakes in hindsight (misplays and rules errors, including ones the auditor caught)
## What this game says about the decks

In the last section judge the GAME against what this group wants from Commander: most games end \
in 4 to 10 rounds; every player gets a stretch where their deck does its thing and matters; \
players interact with each other; power is around mid-to-high bracket 3, not tuned for raw \
strength. Say plainly which decks helped or hurt that, and what you would change first.

Style: {style}
Do not invent anything that is not in the round accounts."""


_APPENDIX = "## Appendix: the round accounts this was written from"


def _accounts_in(path: Path) -> dict[int, str]:
    """The round accounts at the end of an existing narrative, by round number."""
    if not path.is_file() or _APPENDIX not in (text := path.read_text(encoding="utf-8")):
        return {}
    accounts = {}
    for section in text.split(_APPENDIX, 1)[1].split("\n### Round ")[1:]:
        number, _, body = section.partition("\n")
        if number.strip().isdigit():
            accounts[int(number)] = body.strip()
    return accounts


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line] if path.is_file() else []


def _round_material(round_number: int, events: list[dict], wakes: list[dict], audits: list[dict], names: list[str]) -> str:
    turns = sorted({e["turn"] for e in events})
    lines = [f"ROUND {round_number} (table turns {turns[0]} to {turns[-1]})", "", "THE LOG (full information)"]
    for event in events:
        private = "".join(f" [private to {names[int(seat)]}: {' '.join(text)}]" for seat, text in event["private"].items()
                          if text != ["Notes saved."])
        passing = event["tool"] == "pass_priority"  # a pass is noise, but the turn's draw rides on the pass that ended upkeep
        if (passing and not private and not event["lints"]) or not (event["public"] or private or event["lints"]):
            continue
        lines.append(f"- turn {event['turn']} {event['step']}: " + ("" if passing else " ".join(event["public"])) + private)
        lines += [f"  - table warning: {lint}" for lint in event["lints"]]
    accounts = [w for w in wakes if w.get("turn") in turns and w.get("kind") != "triage" and (w.get("summary") or "").strip()]
    if accounts:
        lines += ["", "THE PLAYERS' OWN ACCOUNTS (private; written right after each wake)"]
        lines += [f"- {names[w['seat']]} (turn {w['turn']}, {w['kind']}): {' '.join(w['summary'].split())}" for w in accounts]
    found = [(a["turn"], f) for a in audits if a["turn"] in turns for f in a.get("findings", [])]
    if found:
        lines += ["", "THE RULES AUDITOR'S FINDINGS"]
        lines += [f"- turn {turn}, {f['kind']}: {f['summary']} Fix: {f.get('fix', '')}" for turn, f in found]
    return "\n".join(lines)


def summarize(game_dir: str | Path, runner, database: CardDatabase, *, style: str = "story", echo=print) -> dict:
    """Write `narrative.md` into the game directory. The accounts of finished rounds in an existing
    `narrative.md` are reused, so only a new style or new rounds cost anything."""
    if style not in STYLES:
        raise ValueError(f"The style is one of: {', '.join(STYLES)}.")
    directory = GameDirectory.open(game_dir)
    config = directory.read("config.json")
    names = config.get("seats") or [seat["name"] for seat in config["table"]["seats"]]
    table = Table.replay(config["table"], directory.events(), card_lookup=_lookup(database))  # replaying numbers the rounds
    events = [e for e in table.events if e.get("round")]
    rows = [row for index in range(len(names)) for row in _jsonl(directory.path / f"transcript-seat-{index}.jsonl")]
    wakes = [{"seat": row.get("seat", 0), **row, "turn": row.get("table_turn", row.get("turn"))} for row in rows]
    audits = _jsonl(directory.path / "audits.jsonl")
    existing = _accounts_in(directory.path / "narrative.md")
    accounts = []
    for number in sorted({e["round"] for e in events}):
        in_round = [e for e in events if e["round"] == number]
        finished = number < max(e["round"] for e in events) or table.game.control.kind == "over"
        if number in existing and finished:
            accounts.append((number, existing[number]))
            continue
        echo(f"condensing round {number} ({len(in_round)} events) ...")
        reply = runner.ask(ROUND_SYSTEM, _round_material(number, in_round, wakes, audits, names))
        accounts.append((number, reply.text.strip()))
    game = table.game
    standings = "\n".join(f"- {s.name}: {s.status}, {s.life} life; commander: "
                          + (", ".join(config["table"]["seats"][s.index].get("commanders", [])) or "none") for s in game.seats)
    result = f"Winner: {names[game.winner]}." if game.winner is not None else "The game was stopped before anyone won."
    echo(f"writing the narrative ({style}) from {len(accounts)} round accounts ...")
    reply = runner.ask(STORY_SYSTEM.format(style=STYLES[style]),
                       f"THE POD\n{standings}\n\nTHE RESULT\n{result} {len(accounts)} rounds were played.\n\n"
                       + "\n\n".join(f"ROUND {number}\n{text}" for number, text in accounts))
    directory.write("narrative.md", reply.text.strip() + "\n\n" + _APPENDIX + "\n\n"
                    + "\n\n".join(f"### Round {number}\n\n{text}" for number, text in accounts) + "\n")
    return {"directory": str(directory.path), "rounds": len(accounts), "style": style, "narrative": str(directory.path / "narrative.md")}
