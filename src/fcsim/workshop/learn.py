"""`fcsim deck learn`: turn finished games into piloting lessons for a deck project, and its primer.

Separate from `review`, which judges the list. This asks only how the deck should be played, keeps
each answer with its evidence, and rewrites the primer from what has been borne out. Lessons are
checked against the record by the same second pass as a review, because a lesson built on something
that did not happen is worse than none: it would be taught to every later pilot.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..cards import CardDatabase, read_decklist, resolve
from ..play.gamedir import GameDirectory
from ..play.goldfish import _deck_reference
from .project import PRIMER_WORDS, DeckProject
from .review import CHECK_SCHEMA, CHECK_SYSTEM, _game_material, _list_numbers, _seat_of

KINDS = ["opening_hand", "sequencing", "commander_timing", "mana", "combat", "threat_assessment", "interaction", "politics", "how_a_card_works"]

LESSONS_SYSTEM = """\
You study how one Commander deck was piloted in games played by AI agents, to teach its next \
pilot. You have full information and hindsight: the list, what was measured from each game's log, \
each of this seat's own turns, what happened to every card its pilot saw, an account of every \
round, the pilot's own notes and the rules auditor's findings.

Write piloting lessons: how to PLAY this deck, never what to put in it. Opening hands and \
mulligans, the order to do things in, when to cast the commander, how to spend mana, when to attack \
and with what, which opposing threats to answer and which to leave, what to say at the table, and \
how a card in this list really works where the pilot got it wrong. Each lesson must be specific \
enough to act on ("cast Fire Lord Zuko on turn three or four, before Isshin: every exile payoff \
needs him in play", not "develop your board"); must be something the pilot could have known at the \
time, from its own hand, the board and the cards' text, never from hidden cards or how the game \
turned out; must name the cards it depends on; and must quote its evidence: which game, which \
round, what happened. Lessons about the playing tools themselves are not wanted. Three to eight \
lessons, and fewer if the games support fewer: a lesson taught to every later pilot has to be right.

Start from HOW THIS SEAT WAS PILOTED, MEASURED. It is where a pilot's mistakes show without anyone's \
opinion: the hand it kept (the first mulligan in Commander is free: a seven with two lands and \
nothing cheap is rarely a keep), the land drops it missed afterwards, the turns it ended with mana \
untapped beside a card it could have cast, the turns it had attackers and sent none, or kept them \
home from a player with no untapped blocker. Each of those is a lesson only if the pilot had no good \
reason at the time (a held instant, a swing into first strikers): read what it said it was doing, \
and say which it was. A loss that followed from a hand that should have gone back teaches about \
mulligans, not about the cards that were stuck in it.

Facts. The sections headed MEASURED, THIS SEAT TURN BY TURN, HOW THIS SEAT WAS PILOTED, WHAT HAPPENED TO EACH CARD and THE LIST \
IN NUMBERS are computed from the log and the card data. Never state anything they contradict, and \
do not repeat a claim from a round account or a pilot's note that they contradict. Your lessons are \
checked against them. Answer in the required JSON form."""

LESSONS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["lessons"], "properties": {"lessons": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["claim", "kind", "cards", "evidence"],
    "properties": {"claim": {"type": "string", "description": "The lesson, as an instruction to the next pilot, in one or two sentences."},
                   "kind": {"type": "string", "enum": KINDS}, "cards": {"type": "array", "items": {"type": "string"}},
                   "evidence": {"type": "string", "description": "Which game and round, and what happened."}}}}}}

PRIMER_SYSTEM = """\
You write the primer a pilot reads before every game with one Commander deck. It is short, because \
it is read at the start of every turn's session: {words} words at most. It is advice to a strong \
player who does not know this list, not a description of the deck. Sections, as Markdown headings: \
Gameplan; Opening hands; Early turns and the commander; How it wins; What to answer, what to ignore; \
Mistakes this deck's pilots have made.

You are given the list, the primer as it stands, and the lessons recorded from its games, each with \
its standing. A validated lesson has been borne out in more than one game: teach it plainly. A \
tentative one was seen once: include it only if it is clearly sound from the cards themselves, and \
say "seen once". A stale one leans on a card no longer in the list: leave it out. Where no lesson \
speaks, write from the list itself, and only what the cards' text supports; name cards exactly. \
Keep what is still right in the old primer. Do not mention games, seats, auditors or tools. Answer \
with the primer alone, in Markdown."""


def learn(project_path: str | Path, game_dirs: list[str | Path], runner, database: CardDatabase, *, echo=print) -> dict:
    project = DeckProject(project_path)
    deck = resolve(read_decklist(project.list_path), database, online=True)
    games = []
    for game_dir in game_dirs:
        directory = GameDirectory.open(game_dir)
        seat = _seat_of(directory, [], project.list_path, {card.name for card in deck.commanders})
        if seat is None:
            raise ValueError(f"{project.path.name} did not play in {game_dir}: no seat has its list or its commander.")
        games.append((directory.path, seat))
    materials = "\n\n".join([
        f"THE DECK: {' + '.join(card.name for card in deck.commanders)}",
        "THE LIST IN NUMBERS\n" + _list_numbers(deck), "THE LIST (count, name, cost, type, text)\n" + _deck_reference(deck, database),
        *(["THE PRIMER ITS PILOTS WERE GIVEN\n" + project.primer()] if project.primer() else []),
        *("=" * 60 + "\n" + _game_material(path, seat) for path, seat in games)])
    echo(f"studying {len(games)} game(s) of {project.path.name} ...")
    reply = runner.ask(LESSONS_SYSTEM, f"{materials}\n\nWrite the lessons.", schema=LESSONS_SCHEMA)
    lessons = list((reply.data or {}).get("lessons") or [])
    echo(f"checking {len(lessons)} lesson(s) against the record ...")
    checked = runner.ask(CHECK_SYSTEM, f"{materials}\n\n{'=' * 60}\nTHE REVIEW TO CHECK (a list of piloting lessons; check each one's claim and "
                                       f"evidence)\n{json.dumps(lessons, indent=1, ensure_ascii=False)}", schema=CHECK_SCHEMA)
    problems = list((checked.data or {}).get("problems") or [])
    wrong = [lesson for lesson in lessons if any(p["claim"][:60].lower() in (lesson["claim"] + " " + lesson["evidence"]).lower()
                                                 or lesson["claim"][:60].lower() in p["claim"].lower() for p in problems)]
    kept = [lesson for lesson in lessons if lesson not in wrong]
    counts = project.record(kept, games=[path.name for path, _ in games], database=database)
    standing = project.lessons()
    echo(f"{counts['added']} new, {counts['validated']} borne out again, {len(wrong)} dropped by the fact check; rewriting the primer ...")
    primer = runner.ask(PRIMER_SYSTEM.format(words=PRIMER_WORDS), "\n\n".join([
        f"THE DECK: {' + '.join(card.name for card in deck.commanders)}", "THE LIST (count, name, cost, type, text)\n" + _deck_reference(deck, database),
        "THE PRIMER AS IT STANDS\n" + (project.primer() or "(none yet)"),
        "LESSONS RECORDED\n" + "\n".join(f"- [{'stale' if k.get('stale') else k['status']}, {len(k['games'])} game(s), {k['kind']}] {k['claim']} "
                                         f"(cards: {', '.join(k['cards']) or '-'})" for k in standing),
        "Write the primer."])).text.strip()
    (project.path / "primer.md").write_text(primer + "\n", encoding="utf-8")
    note = project.path / "reviews" / f"{date.today()}-lessons-{len(list((project.path / 'reviews').glob('*-lessons-*'))) + 1}.md"
    note.parent.mkdir(exist_ok=True)
    note.write_text("\n".join([f"# Lessons from {', '.join(path.name for path, _ in games)}", "",
                               *(f"- **{k['kind']}** {k['claim']} _Evidence: {k['evidence']}_" for k in kept), "",
                               "## Dropped by the fact check", "", *([f"- {p['claim']} Record: {p['record']}" for p in problems] or ["- none"])]) + "\n", encoding="utf-8")
    project.log(f"studied {len(games)} game(s): {counts['added']} new lesson(s), {counts['validated']} validated, {len(wrong)} dropped by the fact "
                f"check; primer rewritten ({len(primer.split())} words). See `{note.relative_to(project.path)}`.")
    return {"project": str(project.path), **counts, "dropped": len(wrong), "primer_words": len(primer.split()), "note": str(note)}
