"""`fcsim deck learn`: turn finished games into piloting lessons for a deck project, and its primer.

Separate from `review`, which judges the list. This asks only how the deck should be played, keeps
each answer with its evidence, and rewrites the primer from what has been borne out. Lessons are
checked against the record by the same second pass as a review, because a lesson built on something
that did not happen is worse than none: it would be taught to every later pilot.
"""

from __future__ import annotations

import difflib
import json
from datetime import date
from pathlib import Path

from ..cards import CardDatabase, read_decklist, resolve
from ..play.gamedir import GameDirectory
from ..play.goldfish import _deck_reference
from .project import PRIMER_WORDS, DeckProject, GenericGuide
from .review import CHECK_SYSTEM, _game_material, _list_numbers, _seat_of

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
home from a player with no untapped blocker, and the turns it sent everything and what the table \
took from it before it untapped again. Each of those is a lesson only if the pilot had no good \
reason at the time (a held instant, a swing into first strikers): read what it said it was doing, \
and say which it was. A loss that followed from a hand that should have gone back teaches about \
mulligans, not about the cards that were stuck in it. Under `piloting`, go through every game first \
and say what the record shows of its keep, its mana and its attacks. Wherever that was a mistake, \
one of your lessons must be about it, in this deck's own terms and numbers (how many lands and \
which colours this list needs to function, what it does with its second and third turns), and such \
a lesson need name no card. A pilot that plays its cards cleverly from a hand it should not have \
kept has learned the smaller thing.

LESSONS ALREADY RECORDED lists what earlier studies of this deck taught, each with an id. If a \
lesson you would write is one of those, give its id in `same_as` and restate it only as far as these \
games add to it: a lesson seen again in a different game is how it comes to be trusted, and one \
written twice in other words is only clutter.

Mark a lesson `general` when it would hold for any deck at any Commander table and depends on no \
card of this list: about mulligans, spending mana, when to attack and what to keep home, whom to \
treat as the threat, when to use an answer, what to say at the table. General lessons go to the \
guide every pilot reads, whatever it plays, so that no deck has to learn them again; name no card of \
this list in their claim (the evidence may). GENERAL LESSONS ALREADY RECORDED lists them with ids \
(G001 ...): if yours restates one, give that id in `same_as`. THE GENERAL GUIDE AS IT STANDS is what \
the pilots were already told: a lesson it already teaches is worth recording only when these games \
bore it out again or show it being ignored.

Facts. The sections headed MEASURED, THIS SEAT TURN BY TURN, HOW THIS SEAT WAS PILOTED, WHAT HAPPENED TO EACH CARD and THE LIST \
IN NUMBERS are computed from the log and the card data. Never state anything they contradict, and \
do not repeat a claim from a round account or a pilot's note that they contradict. Your lessons are \
checked against them. Answer in the required JSON form."""

LESSONS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["piloting", "lessons"], "properties": {
    "piloting": {"type": "array", "description": "One entry for each game, before any lesson: what the measured piloting record shows.", "items": {
        "type": "object", "additionalProperties": False, "required": ["game", "keep", "mana", "attacks"],
        "properties": {"game": {"type": "string", "description": "The game's directory name."},
                       "keep": {"type": "string", "description": "The hand that was kept, in numbers, and whether it should have gone back, with the reason."},
                       "mana": {"type": "string", "description": "Land drops missed and turns ended with a castable card and the mana for it: a mistake, or not, and why."},
                       "attacks": {"type": "string", "description": "Turns its attackers stayed home: a mistake, or not, and why."}}}},
    "lessons": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["claim", "kind", "cards", "evidence", "general"],
        "properties": {"claim": {"type": "string", "description": "The lesson, as an instruction to the next pilot, in one or two sentences."},
                       "kind": {"type": "string", "enum": KINDS}, "cards": {"type": "array", "items": {"type": "string"}},
                       "evidence": {"type": "string", "description": "Which game and round, and what happened."},
                       "general": {"type": "boolean", "description": "True if it holds for any deck at any table and names no card of this list."},
                       "same_as": {"type": "string", "description": "The id of the lesson already recorded that this one restates (L004), if it does."}}}}}}

LESSON_CHECK = """

These are lessons, numbered. For each problem give the number of the lesson it is in, and say in \
`stands` whether the lesson still holds once the fact is put right: a count that is off by one or a \
wrong round number does not undo advice that the record otherwise bears out, while a lesson built on \
something that did not happen, or on a card that does not do what it says, falls. Where it stands, \
give the whole claim again in `corrected` with the fact put right."""

LESSON_CHECK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["problems"], "properties": {"problems": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["lesson", "claim", "record", "stands"],
    "properties": {"lesson": {"type": "integer"}, "claim": {"type": "string"}, "record": {"type": "string"}, "stands": {"type": "boolean"},
                   "corrected": {"type": "string", "description": "The lesson's whole claim with the fact put right, if it stands."}}}}}}

GENERIC_WORDS = 600  # a guide, not a cutoff, as for a deck's primer
GENERIC_SYSTEM = """\
You write the guide every pilot reads at the start of every session of every Commander game at this \
table, whatever deck it plays. It comes before the deck's own primer, which knows the deck's cards; \
this guide knows the game: opening hands, spending mana, attacking and what to keep home, reading the \
table and choosing the threat, when to use an answer, what to say. It names no card of any one deck, \
except inside a short account of what happened at this table, which is what makes advice stick. About \
{words} words, and more only where every line earns its place.

Keep what THE GUIDE AS IT STANDS says unless a lesson recorded since contradicts it. Add what the \
validated lessons teach, in full; a tentative one (seen in one game) only briefly, marked as seen \
once, or not at all if it is weak. Say nothing twice. Write for a strong player: concrete, no \
platitudes, in headed sections of short bullets. Reply with the guide in Markdown and nothing else."""

PRIMER_SYSTEM = """\
You write the primer a pilot reads before every game with one Commander deck. It is short, because \
it is read at the start of every turn's session: about {words} words, and more only where every line \
earns its place; a good line is never cut to make the count. It is advice to a strong \
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


def learn(project_path: str | Path, game_dirs: list[str | Path], runner, database: CardDatabase, *, echo=print, rewrite_guide: bool = True) -> dict:
    """`rewrite_guide` False leaves the general guide to the caller: `deck learn all` rewrites it once, after every deck,
    where it was rewritten once a deck, five times in one run."""
    project = DeckProject(project_path)
    generic = GenericGuide(project.path.parent)
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
        *(["THE GENERAL GUIDE AS IT STANDS\n" + generic.primer()] if generic.primer() else []),
        *(["GENERAL LESSONS ALREADY RECORDED\n" + "\n".join(f"- {k['id']} [{k['status']}, from {len(k['games'])} game(s) of {len(k.get('decks', []))} deck(s), "
                                                             f"{k['kind']}] {k['claim']}" for k in generic.lessons())] if generic.lessons() else []),
        *(["LESSONS ALREADY RECORDED\n" + "\n".join(f"- {k['id']} [{k['status']}, from {len(k['games'])} game(s), {k['kind']}] {k['claim']}" for k in project.lessons())]
          if project.lessons() else []),
        *("=" * 60 + "\n" + _game_material(path, seat) for path, seat in games)])
    echo(f"studying {len(games)} game(s) of {project.path.name} ...")
    reply = runner.ask(LESSONS_SYSTEM, f"{materials}\n\nWrite the lessons.", schema=LESSONS_SCHEMA)
    lessons = list((reply.data or {}).get("lessons") or [])
    piloting = [row for row in (reply.data or {}).get("piloting") or [] if isinstance(row, dict)]
    echo(f"checking {len(lessons)} lesson(s) against the record ...")
    numbered = [{"number": index + 1, **lesson} for index, lesson in enumerate(lessons)]
    checked = runner.ask(CHECK_SYSTEM + LESSON_CHECK, f"{materials}\n\n{'=' * 60}\nTHE REVIEW TO CHECK (a list of piloting lessons; check each one's claim and "
                                                      f"evidence)\n{json.dumps(numbered, indent=1, ensure_ascii=False)}", schema=LESSON_CHECK_SCHEMA)
    problems = list((checked.data or {}).get("problems") or [])
    wrong, kept = [], []
    for index, lesson in enumerate(lessons):
        mine = [p for p in problems if p.get("lesson") == index + 1 or (p.get("lesson") is None and (
            p["claim"][:60].lower() in (lesson["claim"] + " " + lesson["evidence"]).lower() or lesson["claim"][:60].lower() in p["claim"].lower()))]
        if any(not p.get("stands") or not str(p.get("corrected") or "").strip() for p in mine):
            wrong.append(lesson)
            continue
        if mine:  # a count off by one, a wrong round: the lesson stands, said with the fact put right
            # The claim is replaced only by a correction of the claim. A problem the check found in the evidence came back with
            # the evidence corrected, and six lessons had their advice replaced by an account of a game that way.
            of_claim = [p for p in mine if difflib.SequenceMatcher(None, str(p["corrected"]).lower(), lesson["claim"].lower()).ratio() >= 0.5]
            lesson = {**lesson, "claim": str(of_claim[-1]["corrected"]).strip() if of_claim else lesson["claim"],
                      "evidence": lesson["evidence"] + " (Put right by the fact check: " + "; ".join(p["record"] for p in mine) + ")"}
        kept.append(lesson)
    general = [lesson for lesson in kept if lesson.get("general")]
    kept_here = [lesson for lesson in kept if not lesson.get("general")]
    counts = project.record(kept_here, games=[path.name for path, _ in games], database=database)
    shared = generic.record(general, games=[path.name for path, _ in games], deck_id=project.deck_id) if general else {"added": 0, "validated": 0}
    if (shared["added"] or shared["validated"]) and rewrite_guide:
        rewrite_generic(generic, runner, echo=echo, why=f"from {project.deck_id}'s games {', '.join(path.name[-5:] for path, _ in games)}")
    standing = project.lessons()
    echo(f"{counts['added']} new, {counts['validated']} borne out again, {len(wrong)} dropped by the fact check; "
         f"{len(general)} general, for every deck ({shared['added']} new, {shared['validated']} borne out again); rewriting the primer ...")
    primer = runner.ask(PRIMER_SYSTEM.format(words=PRIMER_WORDS), "\n\n".join([
        f"THE DECK: {' + '.join(card.name for card in deck.commanders)}", "THE LIST (count, name, cost, type, text)\n" + _deck_reference(deck, database),
        "THE PRIMER AS IT STANDS\n" + (project.primer() or "(none yet)"),
        "LESSONS RECORDED\n" + "\n".join(f"- [{'stale' if k.get('stale') else k['status']}, {len(k['games'])} game(s), {k['kind']}] {k['claim']} "
                                         f"(cards: {', '.join(k['cards']) or '-'})" for k in standing),
        "Write the primer."])).text.strip()
    if len(primer.split()) > PRIMER_WORDS * 1.5:
        # It is read at the start of every session of every game, so length is a cost; but the count is a guide, not a cutoff, and
        # a primer a third over it that earns its lines is kept. Only one half again as long is asked for once more, with the
        # count; what comes back is used whatever its length.
        echo(f"the primer came back at {len(primer.split())} words against a guide of {PRIMER_WORDS}; asking once more ...")
        primer = runner.ask(PRIMER_SYSTEM.format(words=PRIMER_WORDS), f"THE PRIMER YOU WROTE\n{primer}\n\nIt is {len(primer.split())} words against a "
                            f"guide of {PRIMER_WORDS}. Keep opening hands and every lesson that is not marked as seen once, in full, and every line that "
                            "earns its place; shorten or drop what was seen once, and say nothing twice. Write the primer.").text.strip() or primer
    (project.path / "primer.md").write_text(primer + "\n", encoding="utf-8")
    note = project.path / "reviews" / f"{date.today()}-lessons-{len(list((project.path / 'reviews').glob('*-lessons-*'))) + 1}.md"
    note.parent.mkdir(exist_ok=True)
    note.write_text("\n".join([f"# Lessons from {', '.join(path.name for path, _ in games)}", "",
                               *(f"- **{k['kind']}**{' (general: to the guide every pilot reads)' if k.get('general') else ''} {k['claim']} "
                                 f"_Evidence: {k['evidence']}_" for k in kept), "",
                               "## How it was piloted, game by game", "",
                               *([f"- `{row.get('game', '')}` **Keep:** {row.get('keep', '')} **Mana:** {row.get('mana', '')} **Attacks:** {row.get('attacks', '')}"
                                  for row in piloting] or ["- not said"]), "",
                               "## What the fact check found", "", *([f"- ({'put right' if p.get('stands') and p.get('corrected') else 'dropped'}) {p['claim']} Record: {p['record']}"
                                                                       for p in problems] or ["- nothing"])]) + "\n", encoding="utf-8")
    project.log(f"studied {len(games)} game(s): {counts['added']} new lesson(s), {counts['validated']} validated, {len(wrong)} dropped by the fact "
                f"check; primer rewritten ({len(primer.split())} words). See `{note.relative_to(project.path)}`.")
    return {"project": str(project.path), **counts, "general_added": shared["added"], "general_validated": shared["validated"],
            "dropped": len(wrong), "primer_words": len(primer.split()), "note": str(note)}


def rewrite_generic(generic: GenericGuide, runner, *, echo=print, why: str = "") -> str:
    """The guide every pilot reads, rewritten from what it says now and the general lessons on record."""
    echo("rewriting the general guide every pilot reads ...")
    lessons = generic.lessons()
    guide = runner.ask(GENERIC_SYSTEM.format(words=GENERIC_WORDS), "\n\n".join([
        "THE GUIDE AS IT STANDS\n" + (generic.primer() or "(none yet)"),
        "GENERAL LESSONS RECORDED\n" + ("\n".join(f"- [{k['status']}, {len(k['games'])} game(s) of {len(k.get('decks', []))} deck(s), {k['kind']}] {k['claim']} "
                                                   f"(evidence: {' | '.join(k['evidence'])[:400]})" for k in lessons) or "(none)"),
        "Write the guide."])).text.strip()
    if len(guide.split()) > GENERIC_WORDS * 1.5:
        echo(f"the guide came back at {len(guide.split())} words against a guide of {GENERIC_WORDS}; asking once more ...")
        guide = runner.ask(GENERIC_SYSTEM.format(words=GENERIC_WORDS), f"THE GUIDE YOU WROTE\n{guide}\n\nIt is {len(guide.split())} words against a "
                           f"guide of {GENERIC_WORDS}. Keep every lesson not marked as seen once, in full, and every line that earns its place; "
                           "shorten or drop what was seen once, and say nothing twice. Write the guide.").text.strip() or guide
    if not guide:
        return generic.primer()
    generic.path.mkdir(parents=True, exist_ok=True)
    (generic.path / "primer.md").write_text(guide + "\n", encoding="utf-8")
    generic.log(f"guide rewritten {why} ({len(guide.split())} words; {sum(k['status'] == 'validated' for k in lessons)} validated lesson(s) of {len(lessons)}).")
    return guide
