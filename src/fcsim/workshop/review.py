"""`fcsim review`: read how one deck fared in its games, and propose a few swaps.

The reviewer has full information and hindsight. It is told what this group wants from Commander,
so that "better" does not quietly become "stronger" (PLAN.md §12.1), and its swaps are checked
against the card data before anyone sees them. Applying a swap is a separate, human step.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..cards import CardDatabase, read_decklist, resolve
from ..cards.decklist import Decklist, Entry
from ..play.cardlog import card_record
from ..play.pilotlog import pilot_record, render_pilot
from ..play.cardlog import render as render_cards
from ..play.gamedir import GameDirectory
from ..play.goldfish import _deck_reference
from ..play.narrative import _accounts_in
from ..play.rubric import measure, render_turns
from ..play.rubric import render as render_rubric
from .static import analyze

SYSTEM = """\
You review how one Commander deck fared in games played by AI agents, for the person tuning that \
deck. You have full information and hindsight: the list, what was measured from each game's log, \
an account of every round, what happened to every card this deck's pilot saw, the pilot's own \
notes, and the rules auditor's findings.

What this group wants is not the strongest deck. They play at mid-to-high bracket 3, not cEDH. \
A better deck is one that makes better games: most games over in 4 to 10 rounds; every player \
getting a stretch where their deck does its thing and matters; players interacting with each \
other; wins the table could see coming and could have answered. A deck that does nothing is a \
problem, and so is one that ends games before anyone else has played. A change may lower power.

Separate four causes before you blame the list, because a misplayed deck looks like a bad deck:
- deck: structure, mana, dead cards, a gameplan that does not come together, or one that \
comes together so fast nobody else gets a game.
- pilot: misplays, missed lines, poor threat assessment. These are lessons for the pilot, not swaps.
- pod: a bad matchup, or an opponent's deck that warped the game.
- variance: mana screw, flood, the order of the draws. One game proves little about a card that \
was never drawn. Say so rather than guessing.
Discount anything the rules went wrong on: a card caught up in an auditor's correction or a \
judge's ruling says little about itself.

Then propose swaps, {swaps} at most, and none at all if the evidence does not support any: a few \
games are a small sample, and a list that keeps changing can never be measured. Each swap names \
one card to cut that is in the list and one to add that is not, within the commander's color \
identity and legal in Commander; give the exact card name. Give the reason, the evidence (game, \
round, what happened), and the effect you expect on the games. Keep the deck's identity: cut the \
cards that did not pull their weight for this plan, not the pet cards that make it this deck. \
Never cut a commander.

Tell the pilot from the deck. HOW THIS SEAT WAS PILOTED, MEASURED gives the hand that was kept, the \
land drops missed, the turns that ended with mana untapped beside a castable card, and the turns \
attackers stayed home. A card stuck in hand in a game where the pilot kept two lands and never had \
a fourth says nothing against the card; a deck that dealt no damage because its pilot never \
attacked has not shown that it lacks reach. Where a loss was the pilot's, say so under The pilot, \
and propose no swap on its evidence.

Facts. The sections headed MEASURED, THIS SEAT TURN BY TURN, HOW THIS SEAT WAS PILOTED, WHAT HAPPENED TO EACH CARD and THE \
LIST IN NUMBERS are computed from the log and the card data. Never state anything they contradict, \
and do not repeat a claim from a round account or a pilot's note that they contradict: say how many \
lands the deck had from the turn-by-turn lines, and count cards from the list, not from memory. \
Your review is checked against them before the owner sees it.

Answer in the required JSON form. `review` is Markdown for the deck's owner, 300 to 600 words, \
with the headings: What happened, The deck, The pilot, The pod and the draws, What to watch next."""

CHECK_SYSTEM = """\
You are the fact-checker for a review of a Commander deck's games. You are given everything the \
reviewer was given, and the review it wrote. Find every statement of fact in the review, its \
causes, its lessons and its swaps that the record contradicts or does not support: how many lands \
the deck had and when, what was cast or held and when, life totals, who did what, how many cards \
of some kind the list contains, what a card does. The sections computed from the log (MEASURED, \
THIS SEAT TURN BY TURN, HOW THIS SEAT WAS PILOTED, WHAT HAPPENED TO EACH CARD, THE LIST IN NUMBERS) and the card texts in THE \
LIST outrank the round accounts and the pilot's notes. Judgements and recommendations are not \
your business: only facts. For each problem quote the claim and say what the record shows. An \
empty list is the right answer when the review is sound."""

CHECK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["problems"], "properties": {"problems": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["claim", "record"],
    "properties": {"claim": {"type": "string"}, "record": {"type": "string"}}}}}}

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["review", "causes", "lessons", "swaps"],
    "properties": {
        "review": {"type": "string"},
        "causes": {"type": "object", "additionalProperties": False, "required": ["deck", "pilot", "pod", "variance"],
                   "properties": {k: {"type": "string", "description": "One or two sentences; say 'nothing notable' if so."}
                                  for k in ("deck", "pilot", "pod", "variance")}},
        "lessons": {"type": "array", "items": {"type": "string"}, "description": "For the pilot: specific, and knowable at the time."},
        "swaps": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["cut", "add", "reason", "evidence", "expected_effect"],
            "properties": {k: {"type": "string"} for k in ("cut", "add", "reason", "evidence", "expected_effect")}}},
    },
}


def _seat_of(directory: GameDirectory, names: list[str], deck_path: Path, commanders: set[str]) -> int | None:
    """Which seat in this game played the deck: by its path, else by its commander."""
    config = directory.read("config.json")
    for index, path in enumerate(config.get("decks", [])):
        if Path(path).resolve() == deck_path.resolve():
            return index
    for index, seat in enumerate(config["table"]["seats"]):
        if commanders & set(seat.get("commanders", [])):
            return index
    return None


def _game_material(game_dir: Path, seat: int) -> str:
    directory = GameDirectory.open(game_dir)
    config = directory.read("config.json")
    names = config.get("seats") or [s["name"] for s in config["table"]["seats"]]
    accounts = _accounts_in(directory.path / "narrative.md")
    if accounts:
        story = "\n\n".join(f"Round {n}: {text}" for n, text in sorted(accounts.items()))
    else:  # no narrative yet: the table's own log, without the passing
        story = "\n".join(f"[turn {e['turn']} {e['step']}] " + " ".join(e["public"]) for e in directory.events()
                          if e["public"] and e["tool"] not in ("pass_priority", "setup"))
    rows = [json.loads(line) for line in (directory.path / f"transcript-seat-{seat}.jsonl").read_text(encoding="utf-8").splitlines() if line] \
        if (directory.path / f"transcript-seat-{seat}.jsonl").is_file() else []
    said = [f"- turn {r.get('table_turn', r.get('turn'))} ({r['kind']}): {' '.join(r['summary'].split())}" for r in rows
            if r.get("summary") and r.get("kind") not in ("triage", "mulligan")]
    checks = [json.loads(line) for line in (directory.path / "audits.jsonl").read_text(encoding="utf-8").splitlines() if line] \
        if (directory.path / "audits.jsonl").is_file() else []
    found = [f"- turn {a['turn']}, {f['kind']}: {f['summary']}" for a in checks for f in a.get("findings", []) if f.get("seat") == seat]
    found += [f"- turn {a['turn']}, the judge ({a['decision']}): {a['explanation']}" for a in checks if a.get("kind") == "ruling" and a.get("seat") == seat]
    notes = directory.path / f"notes-seat-{seat}.md"
    feedback = directory.path / "feedback.md"
    return "\n\n".join([
        f"GAME {directory.path.name}: this deck sat at seat {seat} as {names[seat]}; the pod was {', '.join(names)}.",
        *([f"THE OWNER'S OWN REACTION TO THIS GAME (the strongest evidence you have)\n{feedback.read_text(encoding='utf-8').strip()}"] if feedback.is_file() else []),
        "MEASURED FROM THE LOG\n" + render_rubric(scored := measure(directory.path)),
        "THIS SEAT TURN BY TURN\n" + render_turns(scored, seat),
        "HOW THIS SEAT WAS PILOTED, MEASURED\n" + render_pilot(pilot_record(directory.path), seat),
        "WHAT HAPPENED TO EACH CARD THE PILOT SAW\n" + render_cards(card_record(directory.path), seat),
        "THE GAME, ROUND BY ROUND\n" + story,
        "WHAT THE PILOT SAID IT WAS DOING\n" + ("\n".join(said) or "(nothing recorded)"),
        "THE PILOT'S NOTES AT THE END\n" + (notes.read_text(encoding="utf-8").strip() if notes.is_file() else "(none)"),
        "RULES FINDINGS ABOUT THIS SEAT\n" + ("\n".join(found) or "(none)")])


def check_swaps(swaps: list[dict], deck, database: CardDatabase) -> tuple[list[dict], list[str]]:
    """The swaps that can be made, with Oracle names, and why each of the others cannot."""
    commanders = {card.name for card in deck.commanders}
    listed = {card.name: card for card, _ in deck.cards}
    good, refused, adding = [], [], set()
    for swap in swaps:
        cut, add = database.find(swap.get("cut", "")), database.find(swap.get("add", ""))
        problem = None
        if cut is None or cut.name not in listed:
            problem = f"{swap.get('cut')!r} is not in the list" + (" (it is the commander)" if cut is not None and cut.name in commanders else "")
        elif add is None:
            hint = ", ".join(database.suggest(swap.get("add", "")))
            problem = f"no card named {swap.get('add')!r}" + (f" (did you mean: {hint}?)" if hint else "")
        elif add.legality != "legal":
            problem = f"{add.name} is not legal in Commander"
        elif set(add.color_identity) - deck.identity:
            problem = f"{add.name} is outside the commander's color identity"
        elif (add.name in listed or add.name in commanders or add.name in adding) and not add.is_basic:
            problem = f"{add.name} is already in the list"
        if problem:
            refused.append(f"cut {swap.get('cut')} for {swap.get('add')}: {problem}")
        else:
            adding.add(add.name)
            good.append({**swap, "cut": cut.name, "add": add.name})
    return good, refused


def _list_numbers(deck) -> str:
    """Counts a reviewer would otherwise make from memory, and get wrong."""
    cards = [card for card, count in deck.cards for _ in range(count)]
    spells = [card for card in cards if not card.is_land]

    def named(found: list) -> str:
        return f"{len(found)}" + (f" ({', '.join(card.name for card in found)})" if 0 < len(found) <= 24 else "")
    lines = [f"{deck.size} cards; {sum(1 for card in cards if card.is_land)} lands; {len(spells)} nonland cards in the 99",
             "nonland cards by mana value: " + ", ".join(f"{value}: {sum(1 for c in spells if min(int(c.mana_value), 7) == value)}" for value in range(8)) + " (7 means 7 or more)"]
    lines += [f"{kind}s: {named([c for c in spells if kind in c.types])}" for kind in ("Instant", "Sorcery", "Creature", "Artifact", "Enchantment", "Planeswalker")]
    lines.append("instants with mana value 2 or less: " + named([c for c in spells if "Instant" in c.types and c.mana_value <= 2]))
    lines.append("nonland cards with mana value 5 or more: " + named([c for c in spells if c.mana_value >= 5]))
    return "\n".join(lines)


def review(deck_path: str | Path, game_dirs: list[str | Path], runner, database: CardDatabase, *, swaps: int = 5,
           out: str | Path | None = None, check: bool = True, echo=print) -> dict:
    """Review the deck at `deck_path` over the games it played in, and write the review with its
    proposed swaps as checkboxes. The default place is beside the list: `<list>.review.md`."""
    deck_path = Path(deck_path)
    deck = resolve(read_decklist(deck_path), database, online=True)
    if deck.unknown or not deck.commanders:
        raise ValueError(f"{deck_path} has unrecognized cards or no commander; run `fcsim deck check` on it first.")
    games = []
    for game_dir in game_dirs:
        directory = GameDirectory.open(game_dir)
        config = directory.read("config.json")
        seat = _seat_of(directory, config.get("seats", []), deck_path, {card.name for card in deck.commanders})
        if seat is None:
            raise ValueError(f"{deck_path.name} did not play in {game_dir}: no seat has its path or its commander.")
        games.append((directory.path, seat))
    analysis = analyze(deck)
    shape = (f"{analysis['lands']['lands']} lands; average mana value {analysis['average_mana_value']}; "
             + ", ".join(f"{role} {row['count']}" for role, row in analysis["roles"].items()))
    echo(f"reviewing {deck_path.name} over {len(games)} game(s) ...")
    materials = "\n\n".join([
        f"THE DECK: {' + '.join(card.name for card in deck.commanders)} ({deck_path.name})\n{shape}",
        "THE LIST IN NUMBERS\n" + _list_numbers(deck),
        "THE LIST (count, name, cost, type, text)\n" + _deck_reference(deck, database),
        *("=" * 60 + "\n" + _game_material(path, seat) for path, seat in games)])
    reply = runner.ask(SYSTEM.format(swaps=swaps), f"{materials}\n\nReview the deck. At most {swaps} swaps.", schema=SCHEMA)
    result, seconds, corrected = reply.data, reply.seconds, []
    if check:
        # A reviewer reads thousands of lines and then writes from its impression of them. In its first
        # real reviews it invented a land stall and miscounted the list; both read as perfectly plausible.
        echo("checking the review's facts against the record ...")
        checked = runner.ask(CHECK_SYSTEM, f"{materials}\n\n{'=' * 60}\nTHE REVIEW TO CHECK\n{json.dumps(result, indent=1, ensure_ascii=False)}",
                             schema=CHECK_SCHEMA)
        corrected, seconds = list((checked.data or {}).get("problems") or []), seconds + checked.seconds
        if corrected:
            echo(f"{len(corrected)} claim(s) did not hold; asking for the review again ...")
            again = runner.ask(SYSTEM.format(swaps=swaps), f"{materials}\n\n{'=' * 60}\nYOUR FIRST DRAFT\n{json.dumps(result, indent=1, ensure_ascii=False)}"
                               "\n\nA FACT-CHECK OF THAT DRAFT FOUND\n" + "\n".join(f"- You wrote: {p['claim']} The record: {p['record']}" for p in corrected)
                               + f"\n\nWrite the review again with these corrected. Drop or rethink any conclusion, lesson or swap that rested on a "
                               f"false claim. At most {swaps} swaps.", schema=SCHEMA)
            result, seconds = again.data, seconds + again.seconds
    good, refused = check_swaps(result.get("swaps", [])[:swaps], deck, database)
    lines = [f"# Review: {' + '.join(card.name for card in deck.commanders)}", "",
             f"List `{deck_path}`; games: " + ", ".join(f"`{path.name}`" for path, _ in games) + ".", "", result["review"].strip(), "",
             "## Causes", "", *(f"- **{cause}**: {result['causes'][cause]}" for cause in ("deck", "pilot", "pod", "variance")), "",
             "## Lessons for the pilot", "", *([f"- {lesson}" for lesson in result.get("lessons", [])] or ["- none"]), "",
             "## Proposed swaps", "",
             "Tick the ones you want, then `fcsim deck apply <list> <this file> -o <new list>`. Edit a line freely: only the "
             "two card names in bold are read.", ""]
    lines += [f"- [ ] cut **{s['cut']}**, add **{s['add']}**: {s['reason']} _Evidence: {s['evidence']} Expected: {s['expected_effect']}_" for s in good]
    lines += ["- none proposed"] if not good else []
    lines += ["", "Refused by the card data:", *(f"- {line}" for line in refused)] if refused else []
    lines += ["", "## Fact check", "", "A second pass checked the first draft against the log and the list, and the review above was rewritten "
              "with these corrected:", "", *(f"- Draft: {p['claim']} Record: {p['record']}" for p in corrected)] if corrected else \
        ["", "## Fact check", "", "A second pass checked this review against the log and the list and found nothing to correct."] if check else []
    path = Path(out) if out else deck_path.with_suffix(".review.md")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"review": str(path), "swaps": good, "refused": refused, "causes": result["causes"], "lessons": result.get("lessons", []),
            "corrected": corrected, "seconds": round(seconds, 1)}


_SWAP = re.compile(r"^- \[(?P<tick>[ xX])\] cut \*\*(?P<cut>.+?)\*\*, add \*\*(?P<add>.+?)\*\*")


def apply(deck_path: str | Path, review_path: str | Path, database: CardDatabase, *, everything: bool = False) -> tuple[Decklist, list[dict], list[str]]:
    """The list with the ticked swaps of a review made. Nothing is written: the caller decides where."""
    decklist = read_decklist(deck_path)
    wanted = [m.groupdict() for line in Path(review_path).read_text(encoding="utf-8").splitlines() if (m := _SWAP.match(line.strip()))]
    wanted = [w for w in wanted if everything or w["tick"].lower() == "x"]
    good, refused = check_swaps(wanted, resolve(decklist, database), database)
    main = list(decklist.main)
    for swap in good:
        index = next(i for i, e in enumerate(main) if (card := database.find(e.name)) is not None and card.name == swap["cut"])
        entry = main[index]
        main[index:index + 1] = ([Entry(entry.count - 1, entry.name, entry.line)] if entry.count > 1 else []) + [Entry(1, swap["add"], 0)]
    return Decklist(commanders=decklist.commanders, main=tuple(main), sideboard=decklist.sideboard, companion=decklist.companion), good, refused
