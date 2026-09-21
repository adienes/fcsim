"""Small local CLI; the read methods can also be bound directly as tools."""

import argparse
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from .agents import BACKENDS, Budget, RunnerError, make_runner
from .audit import Auditor, build, load_scenarios, materials, play, run_suite
from .audit import render as render_report
from .cards import CardDatabase, CardsError, read_decklist
from .cards.database import default_data_dir
from .cards.edhrec import average_deck, random_commanders
from .play import goldfish, play_pod
from .play.aggregate import aggregate
from .play.aggregate import render as render_aggregate
from .play.narrative import STYLES, summarize
from .play.rubric import measure as measure_rubric
from .play.rubric import render as render_rubric
from .play.status import render as render_status
from .play.status import status
from .rules import RulesError, RulesReference
from .workshop.check import check_deck, render
from .workshop.review import apply as apply_review
from .workshop.review import review as review_deck


def _cost_limit(value: str) -> float | None:
    if value.lower() == "none":
        return None
    try:
        amount = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use a positive USD amount or 'none'.") from exc
    if not math.isfinite(amount) or amount <= 0:
        raise argparse.ArgumentTypeError("Use a positive USD amount or 'none'.")
    return amount


def _model_options(parser: argparse.ArgumentParser, *, audited: bool, cost_default: float) -> None:
    parser.add_argument("--backend", choices=BACKENDS, default="claude", help="Model CLI backend (default: claude)")
    parser.add_argument("--model", help="Model ID for this backend (default: Claude Opus 5 or Codex's built-in default)")
    parser.add_argument("--effort", help="Backend reasoning effort, e.g. low, medium, high or xhigh")
    if audited:
        parser.add_argument("--audit-backend", choices=BACKENDS, help="Auditor backend (default: inherit --backend)")
        parser.add_argument("--audit-model", help="Auditor model (default: its backend's default)")
        parser.add_argument("--audit-effort", help="Auditor reasoning effort")
    parser.add_argument("--max-cost", type=_cost_limit, default=argparse.SUPPRESS, metavar="USD|none",
                        help=f"Reported-cost limit (default: {cost_default:g} USD for Claude-only runs, none with Codex)")
    parser.set_defaults(default_max_cost=cost_default)


def _budget(args: argparse.Namespace) -> Budget:
    if args.max_calls <= 0:
        raise ValueError("--max-calls must be positive.")
    backends = {args.backend, getattr(args, "audit_backend", None) or args.backend}
    cost = getattr(args, "max_cost", None if "codex" in backends else args.default_max_cost)
    if "codex" in backends and cost is not None:
        raise ValueError("Codex has no USD estimate; choose --max-cost none and bound the run with --max-calls.")
    return Budget(max_calls=args.max_calls, max_cost_usd=cost)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fcsim")
    commands = parser.add_subparsers(dest="command", required=True)
    rules = commands.add_parser("rules", help="Look up a pinned local rules snapshot")
    rules.add_argument("--directory", help="Rules directory, e.g. games/example/rules")
    rules.add_argument("--version", help="Explicit manifest version (default: the pinned default)")
    rules.add_argument("--as-of", type=date.fromisoformat, help="Check effective date against YYYY-MM-DD")
    rules.add_argument("--allow-future", action="store_true", help="Explicitly permit a not-yet-effective snapshot")
    actions = rules.add_subparsers(dest="action", required=True)
    actions.add_parser("info", help="Verify files and show provenance and entry counts")
    get = actions.add_parser("get", help="Get an exact numbered rule with context")
    get.add_argument("number")
    for name in ("lookup", "search"):
        action = actions.add_parser(name)
        action.add_argument("query")
        action.add_argument("--limit", type=int, default=10)
    section = actions.add_parser("section", help="Browse a numbered section, with pagination")
    section.add_argument("number")
    section.add_argument("--offset", type=int, default=0)
    section.add_argument("--limit", type=int, default=20)
    glossary = actions.add_parser("glossary")
    glossary.add_argument("term")
    export = actions.add_parser("export", help="Copy this pin into a new game/reference directory")
    export.add_argument("destination")

    cards = commands.add_parser("cards", help="Manage the local copy of Scryfall's card data")
    card_actions = cards.add_subparsers(dest="action", required=True)
    card_actions.add_parser("update", help="Download current card data and rebuild the index")
    card_actions.add_parser("info", help="Show where the card data came from and how old it is")
    show = card_actions.add_parser("show", help="Look up one card by name")
    show.add_argument("name")

    deck = commands.add_parser("deck", help="Work with decklists")
    deck_actions = deck.add_subparsers(dest="action", required=True)
    check = deck_actions.add_parser("check", help="Legality, composition and shuffle statistics for a decklist")
    check.add_argument("path")
    check.add_argument("--commander", action="append", default=[], metavar="NAME",
                       help="Name a commander the file does not mark; repeat for partners")
    check.add_argument("--trials", type=int, default=5000, help="Shuffle simulation trials (0 to skip)")
    check.add_argument("--turns", type=int, default=10)
    check.add_argument("--seed", type=int)
    check.add_argument("--json", action="store_true", help="Print the full result as JSON")
    check.add_argument("--verbose", action="store_true", help="List the cards in each role and the policy")
    applied = deck_actions.add_parser("apply", help="Make the ticked swaps of a review; writes a new list, never the old one unless told to")
    applied.add_argument("path")
    applied.add_argument("review")
    where_to = applied.add_mutually_exclusive_group(required=True)
    where_to.add_argument("-o", "--out", help="Where to write the new list")
    where_to.add_argument("--in-place", action="store_true", help="Overwrite the list")
    applied.add_argument("--all", action="store_true", help="Make every proposed swap, ticked or not")
    sampled = deck_actions.add_parser("random", help="Write EDHREC average decks for commanders drawn at random from its hundred most built")
    sampled.add_argument("--count", type=int, default=3)
    sampled.add_argument("--seed", type=int)
    sampled.add_argument("--exclude", action="append", default=[], metavar="COMMANDER", help="Never pick this one; repeat as needed")
    sampled.add_argument("--window", choices=["week", "month", "year"], default="year", help="Popular over the past week, month or two years")
    sampled.add_argument("--out", default="decks", help="Directory to write the lists into")
    average = deck_actions.add_parser("average", help="Write EDHREC's average deck for a commander: a placeholder opponent")
    average.add_argument("commander")
    average.add_argument("--out", help="File to write (default: print)")

    audit = commands.add_parser("audit", help="Measure the rules auditor against written scenarios")
    audit_actions = audit.add_subparsers(dest="action", required=True)
    audit_actions.add_parser("list", help="List the scenarios")
    shown = audit_actions.add_parser("show", help="Print what the auditor is shown for one scenario; no model call")
    shown.add_argument("scenario")
    suite = audit_actions.add_parser("suite", help="Run scenarios through the auditor using a local model CLI login")
    suite.add_argument("--only", nargs="+", metavar="ID", help="Run just these scenarios")
    suite.add_argument("--tier", choices=["ordinary", "weird", "hard"])
    _model_options(suite, audited=False, cost_default=10.0)
    suite.add_argument("--max-calls", type=int, default=40, help="Hard limit on model calls for this run")
    suite.add_argument("--json", action="store_true")
    suite.add_argument("--out", help="Also write the full report, verdicts included, to this JSON file")

    fish = commands.add_parser("goldfish", help="An agent pilots a deck alone, audited")
    fish.add_argument("deck")
    fish.add_argument("--turns", type=int, default=5,
                      help="Pilot turns to play. Past five or so a real game would have had interaction.")
    fish.add_argument("--seed", type=int)
    _model_options(fish, audited=True, cost_default=15.0)
    fish.add_argument("--max-calls", type=int, default=30, help="Hard limit on model calls for this run")
    fish.add_argument("--out", default="games", help="Directory to create the game directory in")

    story = commands.add_parser("summarize", help="Write narrative.md, the story of a game, from its directory (uses the model)")
    story.add_argument("game_dir")
    story.add_argument("--style", choices=sorted(STYLES), default="story")
    story.add_argument("--max-calls", type=int, default=30, help="Hard limit on model calls for this run")
    _model_options(story, audited=False, cost_default=5.0)

    reviewed = commands.add_parser("review", help="Review how a deck fared in its games and propose a few swaps (uses the model)")
    reviewed.add_argument("deck", help="The decklist that was played")
    reviewed.add_argument("game_dirs", nargs="+", help="Games it played in; run `fcsim summarize` on them first for a better review")
    reviewed.add_argument("--swaps", type=int, default=5, help="The most swaps to propose")
    reviewed.add_argument("--out", help="Where to write the review (default: beside the list, as <list>.review.md)")
    reviewed.add_argument("--no-check", action="store_true", help="Skip the second pass that checks the review's facts against the record")
    reviewed.add_argument("--max-calls", type=int, default=5, help="Hard limit on model calls for this run")
    _model_options(reviewed, audited=False, cost_default=5.0)

    several = commands.add_parser("report", help="Several games side by side: rubric verdicts, each deck's showing, each card's; no model call")
    several.add_argument("game_dirs", nargs="+")
    several.add_argument("--deck", help="Also list every card this deck's pilot saw (part of the seat's name)")
    several.add_argument("--json", action="store_true")

    scored = commands.add_parser("rubric", help="Score a game against the table-quality rubric, from its log; no model call")
    scored.add_argument("game_dir")
    scored.add_argument("--json", action="store_true")

    where = commands.add_parser("status", help="Where a game stands and where its time went; works while it is being played")
    where.add_argument("game_dir")
    where.add_argument("--json", action="store_true")

    pod = commands.add_parser("pod", help="A multiplayer game: one agent per deck, audited")
    pod.add_argument("decks", nargs="*", help="Two to four decklists, in turn order after a random start")
    pod.add_argument("--resume", metavar="GAME_DIR", help="Play --cycles more rounds of an existing game instead of starting one")
    pod.add_argument("--audit-every", choices=["turn", "checkpoint"], default="turn",
                     help="Audit each whole turn (default), or every time the pen moves after an action (stricter, much slower)")
    pod.add_argument("--triage-model", help="Model for respond-or-pass questions (default: the players' model)")
    pod.add_argument("--triage-effort", default="low", help="Reasoning effort for respond-or-pass questions (default: low)")
    pod.add_argument("--cycles", type=int, default=3, help="Turns per seat before the game is stopped")
    pod.add_argument("--seed", type=int)
    pod.add_argument("--random-opponents", type=int, default=0, metavar="N",
                     help="Add N opponents drawn at random from EDHREC's most-built commanders (their average lists); with --games, "
                          "each game draws its own")
    pod.add_argument("--games", type=int, default=1, help="Play this many games of the same pod, with seeds counting up from --seed")
    pod.add_argument("--parallel", type=int, default=2, help="With --games, how many to play at once (default 2)")
    _model_options(pod, audited=True, cost_default=40.0)
    pod.add_argument("--max-calls", type=int, default=120, help="Hard limit on model calls for this game")
    pod.add_argument("--out", default="games")

    args = parser.parse_args(argv)
    if args.command in ("goldfish", "pod", "summarize", "review") or (args.command == "audit" and args.action == "suite"):
        _stay_awake()
    if args.command == "pod" and args.games > 1 and not args.resume:
        return _pods(args, list(argv) if argv is not None else sys.argv[1:])
    if args.command in ("cards", "deck", "audit", "goldfish", "pod", "status", "summarize", "rubric", "review", "report"):
        try:
            return {"cards": _cards, "deck": _deck, "audit": _audit, "goldfish": _goldfish, "pod": _pod, "status": _status, "summarize": _summarize, "rubric": _rubric, "review": _review, "report": _report}[args.command](args)
        except (CardsError, RunnerError, ValueError, OSError) as exc:
            print(json.dumps({"error": {"code": "cards_error", "message": str(exc)}}), file=sys.stderr)
            return 2
    try:
        reference = RulesReference(args.directory, version=args.version, as_of=args.as_of,
                                   allow_future=args.allow_future)
        match args.action:
            case "info":
                result = reference.info()
            case "get":
                result = reference.get_rule(args.number)
            case "section":
                result = reference.get_section(args.number, offset=args.offset, limit=args.limit)
            case "glossary":
                result = reference.get_glossary(args.term)
            case "search":
                result = reference.search_rules(args.query, limit=args.limit)
            case "lookup":
                result = reference.lookup_rule(args.query, limit=args.limit)
            case "export":
                result = reference.export_snapshot(args.destination)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (RulesError, OSError) as exc:
        print(json.dumps({"error": {"code": "rules_error", "message": str(exc)}}), file=sys.stderr)
        return 2


def _cards(args: argparse.Namespace) -> int:
    match args.action:
        case "update":
            result = CardDatabase.update()
        case "info":
            result = CardDatabase().info()
        case "show":
            result = CardDatabase().get(args.name).to_dict()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _deck(args: argparse.Namespace) -> int:
    if args.action == "apply":
        decklist, made, refused = apply_review(args.path, args.review, CardDatabase(), everything=args.all)
        for swap in made:
            print(f"cut {swap['cut']}, add {swap['add']}")
        for line in refused:
            print(f"refused: {line}")
        if not made:
            print("No swaps to make: tick the ones you want in the review, or pass --all.")
            return 1
        target = args.path if args.in_place else args.out
        with open(target, "w", encoding="utf-8") as out:
            out.write(decklist.to_mtgo())
        print(f"Wrote {target}; check it with `fcsim deck check {target}`.")
        return 0
    if args.action == "random":
        database, written = CardDatabase(), []
        for pick in random_commanders(args.count, seed=args.seed, exclude=tuple(args.exclude), window=args.window):
            target = os.path.join(args.out, f"{pick['slug']}.txt")
            os.makedirs(args.out, exist_ok=True)
            with open(target, "w", encoding="utf-8") as out:
                out.write(average_deck(pick["name"], page_slug=pick["slug"]))
            checked = check_deck(target, database, trials=0, online=True)
            written.append(target)
            print(f"{pick['name']} ({pick['decks']:,} decks on EDHREC) -> {target}: " + ("legal" if checked["legal"] else
                  "; ".join(issue["message"] for issue in checked["issues"])))
        print(" ".join(written))
        return 0
    if args.action == "average":
        text = average_deck(args.commander)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as out:
                out.write(text)
            print(f"Wrote {args.out}: {sum(int(line.split()[0]) for line in text.splitlines() if line)} cards")
        else:
            print(text, end="")
        return 0
    result = check_deck(args.path, CardDatabase(), commanders=tuple(args.commander), trials=args.trials,
                        turns=args.turns, seed=args.seed, online=True)
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else render(result, verbose=args.verbose))
    return 0 if result["legal"] else 1


def _audit(args: argparse.Namespace) -> int:
    scenarios = load_scenarios()
    if args.action == "list":
        print("\n".join(f"{s['tier']:<9}{s['id']:<36}{s['about']}" for s in scenarios))
        return 0
    database = CardDatabase()
    if args.action == "show":
        scenario = next((s for s in scenarios if s["id"] == args.scenario), None)
        if scenario is None:
            raise ValueError(f"No scenario called {args.scenario!r}; see `fcsim audit list`.")
        table, runner = build(scenario, database)
        before, names, since = Auditor(None, database).snapshot(table)
        play(scenario, runner)
        print(materials(table, before, since, database, names))
        print("\nEXPECTED\n" + json.dumps(scenario["expect"], indent=2))
        return 0
    chosen = [s for s in scenarios if (not args.only or s["id"] in args.only) and (not args.tier or s["tier"] == args.tier)]
    if not chosen:
        raise ValueError("No scenarios match; see `fcsim audit list`.")
    runner = make_runner(backend=args.backend, model=args.model, effort=args.effort, budget=_budget(args))
    report = run_suite(Auditor(runner, database), chosen,
                       progress=None if args.json else lambda r: print(render_report({"results": [r], "summary": None}), flush=True))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as out:
            json.dump(report, out, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render_report({"results": [], "summary": report["summary"]}))
    done = [r for r in report["results"] if "passed" in r]
    return 0 if done and all(r["passed"] for r in done) else 1


def _goldfish(args: argparse.Namespace) -> int:
    budget = _budget(args)
    try:
        rules = RulesReference()
    except RulesError:
        rules = None
    report = goldfish(args.deck, make_runner(args.backend, model=args.model, effort=args.effort, budget=budget, timeout=1800),
                      make_runner(args.audit_backend or args.backend, model=args.audit_model, effort=args.audit_effort,
                                  budget=budget), CardDatabase(),
                      turns=args.turns, seed=args.seed, out_root=args.out, rules=rules)
    with open(f"{report['directory']}/report.md", encoding="utf-8") as text:
        print("\n" + text.read())
    print(f"Everything is in {report['directory']}")
    return 0 if not report["stopped"] else 1


def _pods(args: argparse.Namespace, argv: list[str]) -> int:
    """Several games of one pod, each its own process with its own seed, log and limits; then the
    report over all of them. Separate processes keep the games from sharing anything but the disk."""
    first = args.seed if args.seed is not None else secrets.randbelow(2 ** 31 - args.games)
    passed_on, skip = [], 0
    for word in argv:  # everything but --games, --parallel and --seed is the child's business
        if skip:
            skip -= 1
        elif word in ("--games", "--parallel", "--seed"):
            skip = 1
        elif not word.startswith(("--games=", "--parallel=", "--seed=")):
            passed_on.append(word)

    def one(index: int) -> tuple[int, str | None, int]:
        time.sleep(2 * (index % max(1, args.parallel)))  # game directories are named by the second
        child = subprocess.run([sys.executable, "-c", "import sys; from fcsim.cli import main; sys.exit(main(sys.argv[1:]))",
                                *passed_on, "--seed", str(first + index)], capture_output=True, text=True, check=False)
        where = re.search(r"^Everything is in (.+)$", child.stdout, re.M)
        print(f"game {index + 1} of {args.games} (seed {first + index}): "
              + (where[1] if where else "did not finish: " + (child.stderr.strip().splitlines() or ["no output"])[-1]), flush=True)
        return index, where[1] if where else None, child.returncode
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        results = list(pool.map(one, range(args.games)))
    finished = [where for _, where, _ in results if where]
    if finished:
        print("\n" + render_aggregate(aggregate(finished)))
    return 0 if len(finished) == args.games and not any(code for _, _, code in results) else 1


def _pod(args: argparse.Namespace) -> int:
    if args.resume:
        with open(f"{args.resume}/config.json", encoding="utf-8") as config:
            args.decks = json.load(config)["seats"]  # one runner per seat; the decks are the game's own copies
    elif args.random_opponents:
        mine = tuple(entry.name for path in args.decks for entry in read_decklist(path).commanders)
        for pick in random_commanders(args.random_opponents, seed=args.seed, exclude=mine):
            os.makedirs("decks", exist_ok=True)
            target = os.path.join("decks", f"{pick['slug']}.txt")
            with open(target, "w", encoding="utf-8") as out:
                out.write(average_deck(pick["name"], page_slug=pick["slug"]))
            print(f"opponent: {pick['name']} ({target})", flush=True)
            args.decks.append(target)
    if not args.resume and not 2 <= len(args.decks) <= 4:
        raise ValueError("A pod is two to four decks.")
    budget = _budget(args)
    try:
        rules = RulesReference()
    except RulesError:
        rules = None
    report = play_pod(args.decks, [make_runner(args.backend, model=args.model, effort=args.effort,
                                             budget=budget, timeout=1800) for _ in args.decks],
                      make_runner(args.audit_backend or args.backend, model=args.audit_model, effort=args.audit_effort,
                                  budget=budget), CardDatabase(), cycles=args.cycles,
                      seed=args.seed, out_root=args.out, rules=rules, resume=args.resume, audit_every=args.audit_every,
                      triage_runners=[make_runner(args.backend, model=args.triage_model or args.model, effort=args.triage_effort,
                                                  budget=budget) for _ in args.decks],
                      hook_library=default_data_dir() / "hooks.json")
    with open(f"{report['directory']}/report.md", encoding="utf-8") as text:
        print("\n" + text.read())
    print(f"Everything is in {report['directory']}")
    return 0 if not report["stopped"] else 1


def _stay_awake() -> None:
    """On macOS, keep the machine from idle sleep for as long as this process lives. A laptop on
    battery sleeps within minutes of its owner leaving, and the second pod lost ninety minutes to it:
    model calls simply stop, and the monotonic clock that times them stops too, so nothing shows."""
    if sys.platform == "darwin" and shutil.which("caffeinate") and not os.environ.get("FCSIM_ALLOW_SLEEP"):
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _status(args: argparse.Namespace) -> int:
    report = status(args.game_dir)
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render_status(report))
    return 0


def _review(args: argparse.Namespace) -> int:
    runner = make_runner(args.backend, model=args.model, effort=args.effort, budget=_budget(args))
    result = review_deck(args.deck, args.game_dirs, runner, CardDatabase(), swaps=args.swaps, out=args.out, check=not args.no_check)
    with open(result["review"], encoding="utf-8") as text:
        print("\n" + text.read())
    print(f"Written to {result['review']}")
    return 0


def _report(args: argparse.Namespace) -> int:
    result = aggregate(args.game_dirs)
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else render_aggregate(result, deck=args.deck))
    return 0


def _rubric(args: argparse.Namespace) -> int:
    result = measure_rubric(args.game_dir)
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else render_rubric(result))
    return 0


def _summarize(args: argparse.Namespace) -> int:
    runner = make_runner(args.backend, model=args.model, effort=args.effort, budget=_budget(args))
    result = summarize(args.game_dir, runner, CardDatabase(), style=args.style)
    with open(result["narrative"], encoding="utf-8") as text:
        print("\n" + text.read())
    print(f"Written to {result['narrative']}")
    return 0
