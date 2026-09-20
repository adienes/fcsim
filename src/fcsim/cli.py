"""Small local CLI; the read methods can also be bound directly as tools."""

import argparse
import json
import sys
from datetime import date

from .cards import CardDatabase, CardsError
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

    args = parser.parse_args(argv)
    if args.command in ("cards", "deck"):
        try:
            return _cards(args) if args.command == "cards" else _deck(args)
        except (CardsError, ValueError, OSError) as exc:
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
