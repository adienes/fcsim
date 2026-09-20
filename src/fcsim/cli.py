"""Small local CLI; the read methods can also be bound directly as tools."""

import argparse
import json
import math
import sys
from datetime import date

from .agents import BACKENDS, Budget, RunnerError, make_runner
from .audit import Auditor, build, load_scenarios, materials, play, run_suite
from .audit import render as render_report
from .cards import CardDatabase, CardsError
from .cards.edhrec import average_deck
from .play import goldfish, play_pod
from .play.narrative import STYLES, summarize
from .play.status import render as render_status
from .play.status import status
from .rules import RulesError, RulesReference
from .workshop.check import check_deck, render


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
    _model_options(pod, audited=True, cost_default=40.0)
    pod.add_argument("--max-calls", type=int, default=120, help="Hard limit on model calls for this game")
    pod.add_argument("--out", default="games")

    args = parser.parse_args(argv)
    if args.command in ("cards", "deck", "audit", "goldfish", "pod", "status", "summarize"):
        try:
            return {"cards": _cards, "deck": _deck, "audit": _audit, "goldfish": _goldfish, "pod": _pod, "status": _status, "summarize": _summarize}[args.command](args)
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


def _pod(args: argparse.Namespace) -> int:
    if args.resume:
        with open(f"{args.resume}/config.json", encoding="utf-8") as config:
            args.decks = json.load(config)["seats"]  # one runner per seat; the decks are the game's own copies
    elif not 2 <= len(args.decks) <= 4:
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
                                                  budget=budget) for _ in args.decks])
    with open(f"{report['directory']}/report.md", encoding="utf-8") as text:
        print("\n" + text.read())
    print(f"Everything is in {report['directory']}")
    return 0 if not report["stopped"] else 1


def _status(args: argparse.Namespace) -> int:
    report = status(args.game_dir)
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render_status(report))
    return 0


def _summarize(args: argparse.Namespace) -> int:
    runner = make_runner(args.backend, model=args.model, effort=args.effort, budget=_budget(args))
    result = summarize(args.game_dir, runner, CardDatabase(), style=args.style)
    with open(result["narrative"], encoding="utf-8") as text:
        print("\n" + text.read())
    print(f"Written to {result['narrative']}")
    return 0
