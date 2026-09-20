"""What a decklist says about itself before a card is drawn (PLAN.md §12.3, the free tier).

Roles are guessed from oracle text with regular expressions. They are good enough to count
"about ten pieces of ramp" and not good enough to trust for any single card.
"""

from __future__ import annotations

import re
from collections import Counter

from ..cards.model import COLORS, Card, color_pips
from ..cards.validate import Deck

_BASIC_TYPES = {"Plains": "W", "Island": "U", "Swamp": "B", "Mountain": "R", "Forest": "G"}
_TYPE_ORDER = ("Land", "Creature", "Planeswalker", "Battle", "Artifact", "Enchantment", "Instant", "Sorcery")
_NUMBER = r"(?:a|an|one|two|three|four|five|six|seven|x|\d+)"

_LAND_SEARCH = re.compile(r"search(?:es)? (?:your|their) library for [^.]*?\b(?:land|plains|island|swamp|mountain|forest)\b",
                          re.IGNORECASE)
_ROLE_PATTERNS = {
    "draw": re.compile(rf"\bdraws? {_NUMBER} (?:additional )?cards?\b|\bdraws? cards equal\b|\bdraw that many\b",
                       re.IGNORECASE),
    "removal": re.compile(
        r"\b(?:destroy|exile) (?:up to \w+ )?(?:another )?target (?!land|card|player|spell)[^.]*?"
        r"|\bdeals? (?:\w+|\d+) damage to (?:any target|target creature|target planeswalker|up to)"
        r"|\breturn target (?:nonland )?(?:creature|permanent|artifact|enchantment)[^.]*?to (?:its|their) owner's hand"
        r"|\btarget creature gets -\w+/-\w+|\bfights? (?:up to one )?(?:another )?target",
        re.IGNORECASE),
    "wipe": re.compile(
        r"\b(?:destroy|exile) (?:all|each) (?!lands?\b)[^.]*?(?:creatures?|artifacts?|enchantments?|permanents?|planeswalkers?)"
        r"|\b(?:all|each) creatures? gets? -\w+/-\w+|\bdeals? (?:\w+|\d+) damage to each creature"
        r"|\beach player sacrifices (?:all|\w+) (?:creatures?|permanents?)"
        r"|\breturn all (?:nonland )?(?:creatures?|permanents?)[^.]*?to their owners' hands",
        re.IGNORECASE),
    "counterspell": re.compile(r"\bcounter target\b", re.IGNORECASE),
    "extra_turn": re.compile(r"\bextra turns?\b", re.IGNORECASE),
    "mass_land_denial": re.compile(
        r"\bdestroy all lands\b|\bdestroy all [^.]*?\blands\b|\beach player sacrifices [^.]*?\blands\b"
        r"|\blands don't untap\b|\bnonbasic lands are mountains\b|\bsacrifices? all lands\b",
        re.IGNORECASE),
}
_TUTOR = re.compile(r"search(?:es)? your library for [^.]*?\bcards?\b", re.IGNORECASE)
_EXTRA_LAND = re.compile(r"\bplay (?:an|one|two|\w+) additional lands?\b", re.IGNORECASE)
_LAND_TO_BATTLEFIELD = re.compile(r"\bland[^.]*?onto the battlefield\b", re.IGNORECASE)
_TREASURE = re.compile(r"\bcreates? [^.]*?\bTreasure\b")
_FAST_MANA = frozenset((
    "Sol Ring", "Mana Crypt", "Mana Vault", "Grim Monolith", "Chrome Mox", "Mox Diamond", "Mox Opal",
    "Mox Amber", "Lotus Petal", "Jeweled Lotus", "Lion's Eye Diamond", "Ancient Tomb", "City of Traitors",
    "Gaea's Cradle", "Serra's Sanctum", "Dark Ritual", "Cabal Ritual", "Culling the Weak", "Rite of Flame",
    "Simian Spirit Guide", "Elvish Spirit Guide", "Jeska's Will", "Mishra's Workshop", "Dockside Extortionist",
))


def produced_colors(card: Card, identity: frozenset[str]) -> frozenset[str]:
    """Colors of mana this card can supply to this deck. Fetch lands count for what they find."""
    produced = set(card.produced_mana)
    if not produced and (face := card.land_face) and "search your library" in face.oracle_text.lower():
        named = {color for basic, color in _BASIC_TYPES.items() if basic in face.oracle_text}
        produced = named or set(COLORS)
    return frozenset(produced & (identity | {"C"}))


def is_mana_source(card: Card) -> bool:
    """A nonland permanent that taps (or otherwise works) for mana: a rock or a dork."""
    return not card.is_land and card.is_permanent and bool(card.produced_mana)


def roles(card: Card) -> set[str]:
    """Heuristic roles. A card can have several, or none."""
    text = card.text
    found = {role for role, pattern in _ROLE_PATTERNS.items() if pattern.search(text)}
    finds_land = bool(_LAND_SEARCH.search(text))
    if not card.is_land and (is_mana_source(card) or _EXTRA_LAND.search(text) or _TREASURE.search(text)
                             or (finds_land and _LAND_TO_BATTLEFIELD.search(text))):
        found.add("ramp")
    if _TUTOR.search(text) and not finds_land and not card.is_land:
        found.add("tutor")
    if card.name in _FAST_MANA or (is_mana_source(card) and card.mana_value == 0):
        found.add("fast_mana")
    if card.game_changer:
        found.add("game_changer")
    return found


def primary_type(card: Card) -> str:
    return next((kind for kind in _TYPE_ORDER if kind in card.types), "Other")


def analyze(deck: Deck) -> dict:
    """Composition, curve, color balance, roles and power signals, as a JSON-compatible dict."""
    identity = deck.identity
    everything = [(card, 1) for card in deck.commanders] + list(deck.cards)
    total = sum(count for _, count in everything)

    types: Counter[str] = Counter()
    curve: Counter[int] = Counter()
    pips = dict.fromkeys(COLORS, 0.0)
    land_sources: Counter[str] = Counter()
    other_sources: Counter[str] = Counter()
    by_role: dict[str, list[str]] = {}
    spells = mana_value_sum = lands = flexible_lands = 0
    for card, count in everything:
        types[primary_type(card)] += count
        if card.is_land:
            lands += count
        else:
            spells += count
            mana_value_sum += card.mana_value * count
            curve[min(int(card.mana_value), 7)] += count
            flexible_lands += count if card.land_face else 0
            for color, amount in color_pips(card.front.mana_cost).items():
                pips[color] += amount * count
        if card.land_face or is_mana_source(card):
            target = land_sources if card.is_land else other_sources
            for color in produced_colors(card, identity):
                target[color] += count
        for role in roles(card):
            by_role.setdefault(role, []).extend([card.name] * count)

    pip_total = sum(pips.values())
    source_total = sum(land_sources[c] for c in COLORS)
    colors = {}
    for color in COLORS:
        if pips[color] or land_sources[color] or color in identity:
            colors[color] = {
                "pips": round(pips[color], 1),
                "pip_share": round(pips[color] / pip_total, 3) if pip_total else 0.0,
                "land_sources": land_sources[color],
                "land_source_share": round(land_sources[color] / source_total, 3) if source_total else 0.0,
                "other_sources": other_sources[color],
            }

    prices = [(card.price_usd, count) for card, count in everything]
    signals = ("game_changer", "fast_mana", "tutor", "extra_turn", "mass_land_denial")
    return {
        "cards": total,
        "commanders": [card.name for card in deck.commanders],
        "color_identity": [c for c in COLORS if c in identity],
        "types": {kind: types[kind] for kind in (*_TYPE_ORDER, "Other") if types[kind]},
        "lands": {"lands": lands, "modal_land_faces": flexible_lands,
                  "colorless_only": sum(n for card, n in everything if card.is_land
                                        and produced_colors(card, identity) <= {"C"})},
        "curve": {("7+" if value == 7 else str(value)): curve[value] for value in range(8)},
        "average_mana_value": round(mana_value_sum / spells, 2) if spells else 0.0,
        "colors": colors,
        "roles": {role: {"count": len(names), "cards": sorted(names)}
                  for role, names in sorted(by_role.items()) if role not in signals},
        # Reported for information. Nothing is rejected for them (PLAN.md §12.1).
        "power_signals": {role: {"count": len(by_role.get(role, [])), "cards": sorted(by_role.get(role, []))}
                          for role in signals},
        "price_usd": {"total": round(sum(p * n for p, n in prices if p), 2),
                      "unpriced_cards": sum(n for p, n in prices if not p)},
        "notes": _notes(lands + flexible_lands / 2, by_role, colors),
    }


def _notes(effective_lands: float, by_role: dict[str, list[str]], colors: dict) -> list[str]:
    """Gentle observations against common Commander deckbuilding ranges; none is a rule."""
    notes = []
    if effective_lands < 33:
        notes.append(f"{effective_lands:g} lands is low; most decks at this power run 35 to 38.")
    elif effective_lands > 40:
        notes.append(f"{effective_lands:g} lands is high unless the deck has many ways to use them.")
    for role, low, label in (("ramp", 8, "ramp"), ("draw", 8, "card draw")):
        if len(by_role.get(role, [])) < low:
            notes.append(f"Only {len(by_role.get(role, []))} pieces of {label} were recognized; around ten is typical.")
    interaction = sum(len(by_role.get(role, [])) for role in ("removal", "counterspell", "wipe"))
    if interaction < 8:
        notes.append(f"Only {interaction} pieces of interaction were recognized. Interactive games need "
                     "answers on every side of the table.")
    for color, row in colors.items():
        if row["pip_share"] >= 0.15 and row["land_source_share"] + 0.10 < row["pip_share"]:
            notes.append(f"{color} is {row['pip_share']:.0%} of the colored costs but only "
                         f"{row['land_source_share']:.0%} of the colored land sources.")
    return notes
