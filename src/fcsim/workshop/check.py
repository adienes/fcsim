"""`fcsim deck check`: legality, static analysis and shuffle simulation for one decklist."""

from __future__ import annotations

from pathlib import Path

from ..cards import CardDatabase, read_decklist, resolve, validate_deck
from .shuffle import simulate
from .static import analyze


def check_deck(path: str | Path, database: CardDatabase, *, commanders: tuple[str, ...] = (),
               trials: int = 5000, turns: int = 10, seed: int | None = None, online: bool = False) -> dict:
    decklist = read_decklist(path, commanders=commanders)
    deck = resolve(decklist, database, online=online)
    issues = validate_deck(deck, database)
    result = {
        "deck": str(path),
        "card_data": database.info()["scryfall_updated_at"],
        "legal": not any(issue.severity == "error" for issue in issues),
        "issues": [issue.to_dict() for issue in issues],
        "parser_notes": [*decklist.notes, *(f"Read {written!r} as {name}, its Oracle name." for written, name in deck.aliases)],
        "analysis": analyze(deck),
    }
    # Shuffling a deck with missing or unknown cards would give numbers about a different deck.
    if deck.commanders and not deck.unknown and len(deck.library()) >= 7 + turns and trials:
        result["shuffle_simulation"] = simulate(deck, trials=trials, turns=turns, seed=seed)
    return result


def _bar(value: float, scale: float, width: int = 28) -> str:
    return "█" * round(width * value / scale) if scale else ""


def render(result: dict, *, verbose: bool = False) -> str:
    """A terminal report. The JSON form has the same content and more digits."""
    analysis = result["analysis"]
    out = [f"{' + '.join(analysis['commanders']) or 'No commander'}  "
           f"[{''.join(analysis['color_identity']) or 'colorless'}]  {analysis['cards']} cards  "
           f"${analysis['price_usd']['total']:,.0f}",
           f"{result['deck']}  (card data {result['card_data'][:10]})", ""]

    out.append("LEGALITY  " + ("legal" if result["legal"] else f"{len(result['issues'])} problems"))
    out += [f"  {issue['severity']}: {issue['message']}" for issue in result["issues"]]
    out += [f"  note: {note}" for note in result["parser_notes"]]

    out += ["", "COMPOSITION  " + "  ".join(f"{kind} {n}" for kind, n in analysis["types"].items())]
    lands = analysis["lands"]
    out.append(f"  {lands['lands']} lands ({lands['colorless_only']} colorless-only)"
               + (f" plus {lands['modal_land_faces']} modal cards with a land face" if lands["modal_land_faces"] else ""))

    out += ["", f"CURVE  average mana value {analysis['average_mana_value']} (nonland)"]
    tallest = max(analysis["curve"].values(), default=0)
    out += [f"  {value:>2} {_bar(n, tallest)} {n}" for value, n in analysis["curve"].items()]

    if analysis["colors"]:
        out += ["", "COLORS     cost share   land sources   other sources"]
        out += [f"  {color}        {row['pip_share']:>6.0%}   {row['land_sources']:>6} ({row['land_source_share']:.0%})"
                f"   {row['other_sources']:>6}" for color, row in analysis["colors"].items()]

    out += ["", "ROLES  (guessed from card text)"]
    for role, row in analysis["roles"].items():
        out.append(f"  {role:<13}{row['count']:>3}" + (f"  {', '.join(row['cards'])}" if verbose else ""))
    out += ["", "POWER SIGNALS  (for information; nothing is enforced)"]
    for role, row in analysis["power_signals"].items():
        if row["count"]:
            out.append(f"  {role.replace('_', ' '):<17}{row['count']:>3}  {', '.join(row['cards'])}")
    if not any(row["count"] for row in analysis["power_signals"].values()):
        out.append("  none")
    out += [f"  note: {note}" for note in analysis["notes"]]

    if sim := result.get("shuffle_simulation"):
        hand = sim["opening_hand"]
        out += ["", f"SHUFFLE SIMULATION  {sim['trials']:,} trials",
                f"  kept the first seven {hand['kept_first_seven']:.0%}; mulligans "
                + "  ".join(f"{k}: {v:.0%}" for k, v in hand["mulligans"].items()),
                "  turn   lands   land drop   mana   all colors"]
        out += [f"  {row['turn']:>4}   {row['average_lands']:>5.2f}   {row['made_land_drop']:>9.0%}"
                f"   {row['average_mana']:>4.1f}   {row['all_colors']:>10.0%}" for row in sim["by_turn"]]
        for commander in sim["commanders"]:
            by = ", ".join(f"turn {t}: {p:.0%}" for t, p in commander["castable_by_turn"].items())
            out.append(f"  {commander['name']} (mana value {commander['mana_value']}) castable by {by}")
        out.append(f"  stuck on two lands on turn four {sim['stuck_on_two_lands_turn_four']:.0%}; "
                   f"flooded {sim['flooded']['rate']:.0%} ({sim['flooded']['definition']})")
        if verbose:
            out += ["  policy:"] + [f"    - {line}" for line in sim["policy"]]
        else:
            out.append("  These numbers describe a fixed, stated policy; --verbose prints it.")
    else:
        out += ["", "SHUFFLE SIMULATION  skipped until every card is recognized and a commander is set"]
    return "\n".join(out)
