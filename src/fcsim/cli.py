"""Small local CLI; the read methods can also be bound directly as tools."""

import argparse
import json
import sys
from datetime import date

from .agents import Budget, ClaudeCodeRunner, RunnerError
from .audit import Auditor, build, load_scenarios, materials, play, run_suite
from .audit import render as render_report
from .cards import CardDatabase, CardsError
from .play import goldfish
from .rules import RulesError, RulesReference
from .workshop.check import check_deck, render


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

    audit = commands.add_parser("audit", help="Measure the rules auditor against written scenarios")
    audit_actions = audit.add_subparsers(dest="action", required=True)
    audit_actions.add_parser("list", help="List the scenarios")
    shown = audit_actions.add_parser("show", help="Print what the auditor is shown for one scenario; no model call")
    shown.add_argument("scenario")
    suite = audit_actions.add_parser("suite", help="Run scenarios through the auditor (uses your Claude Code login)")
    suite.add_argument("--only", nargs="+", metavar="ID", help="Run just these scenarios")
    suite.add_argument("--tier", choices=["ordinary", "weird", "hard"])
    suite.add_argument("--model", default="claude-opus-5")
    suite.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    suite.add_argument("--max-calls", type=int, default=40, help="Hard limit on model calls for this run")
    suite.add_argument("--max-cost", type=float, default=10.0, help="Hard limit on estimated cost at list prices, USD")
    suite.add_argument("--json", action="store_true")
    suite.add_argument("--out", help="Also write the full report, verdicts included, to this JSON file")

    fish = commands.add_parser("goldfish", help="An agent pilots a deck alone, audited (uses your Claude Code login)")
    fish.add_argument("deck")
    fish.add_argument("--turns", type=int, default=10)
    fish.add_argument("--seed", type=int)
    fish.add_argument("--model", default="claude-opus-5", help="The pilot's model")
    fish.add_argument("--audit-model", default="claude-opus-5")
    fish.add_argument("--max-calls", type=int, default=30, help="Hard limit on model calls for this run")
    fish.add_argument("--max-cost", type=float, default=15.0, help="Hard limit on estimated cost at list prices, USD")
    fish.add_argument("--out", default="games", help="Directory to create the game directory in")

    args = parser.parse_args(argv)
    if args.command in ("cards", "deck", "audit", "goldfish"):
        try:
            return {"cards": _cards, "deck": _deck, "audit": _audit, "goldfish": _goldfish}[args.command](args)
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
    result = check_deck(args.path, CardDatabase(), commanders=tuple(args.commander), trials=args.trials,
                        turns=args.turns, seed=args.seed)
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
    runner = ClaudeCodeRunner(model=args.model, effort=args.effort,
                              budget=Budget(max_calls=args.max_calls, max_cost_usd=args.max_cost))
    report = run_suite(Auditor(runner, database), chosen,
                       progress=None if args.json else lambda r: print(render_report({"results": [r], "summary": None}), flush=True))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as out:
            json.dump(report, out, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render_report({"results": [], "summary": report["summary"]}))
    done = [r for r in report["results"] if "passed" in r]
    return 0 if done and all(r["passed"] for r in done) else 1


def _goldfish(args: argparse.Namespace) -> int:
    budget = Budget(max_calls=args.max_calls, max_cost_usd=args.max_cost)
    try:
        rules = RulesReference()
    except RulesError:
        rules = None
    report = goldfish(args.deck, ClaudeCodeRunner(model=args.model, budget=budget, timeout=1800),
                      ClaudeCodeRunner(model=args.audit_model, budget=budget), CardDatabase(),
                      turns=args.turns, seed=args.seed, out_root=args.out, rules=rules)
    with open(f"{report['directory']}/report.md", encoding="utf-8") as text:
        print("\n" + text.read())
    print(f"Everything is in {report['directory']}")
    return 0 if not report["stopped"] else 1
