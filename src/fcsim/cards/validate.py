"""Resolve a decklist against the card database and check it against the Commander rules.

Only legality is checked here. Whether a deck is right for a pod is a question for the
table-quality rubric, not for a validator (PLAN.md §12.1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .database import CardDatabase
from .decklist import Decklist, Entry
from .model import Card

_ANY_NUMBER = re.compile(r"A deck can have any number of cards named")
_UP_TO = re.compile(r"A deck can have up to (\w+) cards named")
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10}


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" breaks a Commander rule; "warning" deserves a look
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class Deck:
    """A decklist with every name resolved to a card. `cards` excludes the commanders."""
    commanders: tuple[Card, ...]
    cards: tuple[tuple[Card, int], ...]
    unknown: tuple[Entry, ...] = ()
    aliases: tuple[tuple[str, str], ...] = ()  # (the name as written, the Oracle name it was read as)

    @property
    def identity(self) -> frozenset[str]:
        return frozenset(color for card in self.commanders for color in card.color_identity)

    @property
    def size(self) -> int:
        return len(self.commanders) + sum(count for _, count in self.cards)

    def library(self) -> list[Card]:
        """One entry per physical card in the library."""
        return [card for card, count in self.cards for _ in range(count)]


def resolve(decklist: Decklist, database: CardDatabase, *, online: bool = False) -> Deck:
    """With `online`, a name the local data lacks may be looked up at Scryfall as an alternate name."""
    unknown: list[Entry] = []
    aliases: list[tuple[str, str]] = []

    def lookup(entries: tuple[Entry, ...]) -> list[tuple[Card, int]]:
        found: dict[str, tuple[Card, int]] = {}
        for entry in entries:
            card = database.find(entry.name)
            if card is None and (card := database.alias(entry.name, online=online)) is not None:
                aliases.append((entry.name, card.name))
            if card is None:
                unknown.append(entry)
            else:  # two spellings of one card are still one card
                found[card.oracle_id] = (card, found.get(card.oracle_id, (card, 0))[1] + entry.count)
        return list(found.values())

    commanders = lookup(decklist.commanders)
    return Deck(commanders=tuple(card for card, _ in commanders), cards=tuple(lookup(decklist.main)),
                unknown=tuple(unknown), aliases=tuple(aliases))


def _can_be_commander(card: Card, *, paired: bool) -> bool:
    front = card.front
    supertypes_and_types = front.type_line.split("—")[0].split()
    if "can be your commander" in card.text:
        return True
    if "Legendary" not in supertypes_and_types:
        return False
    if "Creature" in supertypes_and_types:
        return True
    if "Background" in front.type_line:
        return paired  # only beside a commander that chooses a Background
    # Legendary Vehicles and Spacecraft qualify when they have power and toughness.
    return bool({"Vehicle", "Spacecraft"} & set(front.type_line.split())) and front.power is not None


def _pairing_problem(first: Card, second: Card) -> str | None:
    """Why these two cannot share the command zone, or None if they can."""
    def has(card: Card, keyword: str) -> bool:
        return keyword in card.keywords

    def partner_with(card: Card) -> str | None:
        match = re.search(r"Partner with ([^\n(]+)", card.text)
        return match[1].strip() if match else None

    for a, b in ((first, second), (second, first)):
        if partner_with(a):
            if partner_with(a) == b.name:
                return None
            continue
        if has(a, "Choose a background") and "Background" in b.front.type_line:
            return None
        if has(a, "Doctor's companion") and "Time Lord Doctor" in b.front.type_line:
            return None
    def variant(card: Card) -> str | None:  # "Partner—Survivors" pairs only with the same variant
        match = re.search(r"Partner—([^\n(]+)", card.text)
        return match[1].strip() if match else None

    if variant(first) or variant(second):
        return None if variant(first) == variant(second) else \
            f"{first.name} and {second.name} have different Partner variants."
    for keyword in ("Friends forever", "Partner"):
        if has(first, keyword) and has(second, keyword) and not partner_with(first) and not partner_with(second):
            return None
    return f"{first.name} and {second.name} have no ability that lets them be commanders together."


def _copy_limit(card: Card) -> int | None:
    """How many copies the card allows; None means any number."""
    if card.is_basic or _ANY_NUMBER.search(card.text):
        return None
    if match := _UP_TO.search(card.text):
        return _NUMBER_WORDS.get(match[1].lower(), 1)
    return 1


def validate_deck(deck: Deck, database: CardDatabase | None = None) -> list[Issue]:
    """Every Commander deck construction rule the deck breaks."""
    issues: list[Issue] = []

    def error(code: str, message: str) -> None:
        issues.append(Issue("error", code, message))

    for entry in deck.unknown:
        hint = ", ".join(database.suggest(entry.name)) if database else ""
        where = f"line {entry.line}: " if entry.line else ""
        error("unknown_card", f"{where}no card named {entry.name!r}." + (f" Did you mean: {hint}?" if hint else ""))

    if not deck.commanders:
        error("no_commander", "No commander found. Put it after a blank line at the end of the list, "
                              "under a 'Commander' header, or pass --commander.")
    elif len(deck.commanders) > 2:
        error("too_many_commanders", f"{len(deck.commanders)} commanders: "
              + ", ".join(card.name for card in deck.commanders) + ".")
    for card in deck.commanders:
        if not _can_be_commander(card, paired=len(deck.commanders) == 2):
            error("not_a_commander", f"{card.name} ({card.front.type_line}) cannot be a commander.")
    if len(deck.commanders) == 2 and (problem := _pairing_problem(*deck.commanders)):
        error("commander_pairing", problem)

    unrecognized = sum(entry.count for entry in deck.unknown)
    if deck.size + unrecognized != 100:
        counted = f"{deck.size + unrecognized} cards" + (f" ({unrecognized} unrecognized)" if unrecognized else "")
        error("deck_size", f"The deck has {counted} including commanders; Commander decks have exactly 100.")

    commander_ids = {card.oracle_id for card in deck.commanders}
    for card, count in deck.cards:
        limit = _copy_limit(card)
        in_zone = card.oracle_id in commander_ids
        if limit is not None and count + in_zone > limit:
            error("singleton", f"{count + in_zone} copies of {card.name}; the limit is {limit}.")

    everything = list(deck.commanders) + [card for card, _ in deck.cards]
    for card in everything:
        if card.legality == "banned":
            error("banned", f"{card.name} is banned in Commander.")
        elif card.legality != "legal":
            error("not_legal", f"{card.name} is not legal in Commander.")
    if deck.commanders:
        identity = deck.identity
        for card, _ in deck.cards:
            if extra := set(card.color_identity) - identity:
                error("color_identity", f"{card.name} has {'/'.join(sorted(extra))} in its color identity, "
                                        f"outside the commander's {'/'.join(sorted(identity)) or 'colorless'}.")
    return issues
