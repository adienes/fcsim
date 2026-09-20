"""Shuffle a deck thousands of times and count what the opening turns look like.

No model is involved, so these are real statistics, but only about the policy below. The
results say how a deck's mana behaves under that policy, not how a thoughtful pilot does.
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass
from statistics import median

from ..cards.model import Card, color_pips
from ..cards.validate import Deck
from .static import is_mana_source, produced_colors, roles

POLICY = (
    "London mulligan with the first one free. Keep seven cards with 3-5 lands, or with 2 lands and "
    "a mana source costing two or less; after a mulligan keep 2-5 lands; always keep at four cards. "
    "Bottom surplus lands beyond four, then the most expensive spells.",
    "A modal card with a land face is played as a land only when no other land is in hand.",
    "Each turn: draw (every player draws on turn one), play a land that adds a missing color if "
    "possible, then cast the cheapest mana rocks, mana creatures and land-ramp spells that are "
    "affordable. Nothing else is cast, so mana is what the deck could have, not what it would spend.",
    "Lands that always enter tapped give mana from the next turn, as do mana creatures and ramped "
    "lands. Conditionally tapped lands (shock lands, check lands) are treated as untapped.",
    "A cost is payable when total mana covers it and, for each color, enough sources make that "
    "color. Sources are not matched to costs exactly, so multicolor costs read slightly easy.",
)
_ALWAYS_TAPPED = re.compile(r"enters (?:the battlefield )?tapped(?! unless| if)", re.IGNORECASE)
_CONDITIONAL = re.compile(r"you may pay|unless|if you control|as .{1,40} enters", re.IGNORECASE)
_ADD_SYMBOLS = re.compile(r"Add ((?:\{[^}]+\})+)")
_ADD_WORDS = re.compile(r"Add (two|three|four|five) mana", re.IGNORECASE)
_WORD_VALUES = {"two": 2, "three": 3, "four": 4, "five": 5}


@dataclass(frozen=True)
class SimCard:
    """The handful of facts the simulation needs about a card."""
    name: str
    kind: str  # land, flex (modal land face), rock, dork, ramp_spell or other
    mana_value: int
    pips: tuple[tuple[str, int], ...]
    colors: frozenset[str]
    amount: int
    tapped: bool

    @classmethod
    def of(cls, card: Card, identity: frozenset[str]) -> SimCard:
        face = card.land_face
        if card.is_land:
            kind = "land"
        elif "ramp" in roles(card) and not card.is_permanent:
            kind = "ramp_spell"
        elif is_mana_source(card):
            kind = "dork" if "Creature" in card.types else "rock"
        else:
            kind = "flex" if face else "other"
        text = face.oracle_text if face and kind in ("land", "flex") else card.text
        colors = produced_colors(card, identity) if kind != "ramp_spell" else frozenset(identity or {"C"})
        if kind in ("land", "flex") and not colors:
            colors = frozenset({"C"})
        symbols = max((len(re.findall(r"\{", m)) for m in _ADD_SYMBOLS.findall(text)), default=1)
        words = max((_WORD_VALUES[w.lower()] for w in _ADD_WORDS.findall(text)), default=1)
        tapped = bool(_ALWAYS_TAPPED.search(text)) and not _CONDITIONAL.search(text)
        return cls(card.name, kind, int(card.mana_value), _pips(card), colors,
                   amount=max(symbols, words) if kind in ("land", "flex", "rock", "dork") else 1,
                   tapped=tapped or kind in ("dork", "ramp_spell"))


def _pips(card: Card) -> tuple[tuple[str, int], ...]:
    return tuple((color, math.ceil(n)) for color, n in color_pips(card.front.mana_cost).items() if n)


def _lands(hand: list[SimCard]) -> int:
    pure = sum(card.kind == "land" for card in hand)
    return pure + (sum(card.kind == "flex" for card in hand) if pure < 3 else 0)


def _keeps(hand: list[SimCard], mulligans: int) -> bool:
    lands = _lands(hand)
    if mulligans >= 3:
        return True
    if mulligans:
        return 2 <= lands <= 5
    cheap_source = any(card.kind in ("rock", "dork") and card.mana_value <= 2 for card in hand)
    return 3 <= lands <= 5 or (lands == 2 and cheap_source)


def _bottom(hand: list[SimCard], count: int) -> list[SimCard]:
    hand = list(hand)
    for _ in range(count):
        lands = [card for card in hand if card.kind == "land"]
        spells = [card for card in hand if card.kind != "land"]
        if len(lands) > 4 or not spells:
            hand.remove(min(lands, key=lambda card: (len(card.colors), not card.tapped)))
        else:
            hand.remove(max(spells, key=lambda card: card.mana_value))
    return hand


def _payable(cost: int, pips: tuple[tuple[str, int], ...], ready: list[SimCard], spent: int) -> bool:
    if sum(card.amount for card in ready) - spent < cost:
        return False
    return all(sum(card.amount for card in ready if color in card.colors) >= needed for color, needed in pips)


def simulate(deck: Deck, *, trials: int = 5000, turns: int = 10, seed: int | None = None) -> dict:
    """Opening hands, land drops, mana and colors by turn, and when each commander is castable."""
    if trials < 1 or not 1 <= turns <= 20:
        raise ValueError("trials must be positive and turns between 1 and 20.")
    rng = random.Random(seed)
    identity = deck.identity
    library = [SimCard.of(card, identity) for card in deck.library()]
    if len(library) < 7 + turns:
        raise ValueError(f"The library has only {len(library)} cards; too few to simulate {turns} turns.")
    commanders = [(card.name, int(card.mana_value), _pips(card)) for card in deck.commanders]
    wanted = frozenset(identity)

    kept_without_mulligan = 0
    mulligan_counts = [0, 0, 0, 0]
    hand_lands = [0] * 8
    lands_in_play = [0] * turns
    land_drop_made = [0] * turns
    mana_available = [0] * turns
    all_colors = [0] * turns
    first_castable: list[list[int]] = [[] for _ in commanders]
    stuck_on_two = flooded = 0

    for _ in range(trials):
        mulligans = 0
        while True:
            rng.shuffle(library)
            hand = library[:7]
            if _keeps(hand, mulligans):
                break
            mulligans += 1
        kept_without_mulligan += mulligans == 0
        mulligan_counts[min(mulligans, 3)] += 1
        hand = _bottom(hand, max(0, mulligans - 1))
        hand_lands[sum(card.kind == "land" for card in hand)] += 1

        battlefield: list[SimCard] = []
        arriving: list[SimCard] = []  # gives mana from next turn
        lands_seen = sum(card.kind == "land" for card in hand)
        lands_played = 0
        cards_seen = len(hand)
        castable_on = [0] * len(commanders)
        for turn in range(turns):
            battlefield += arriving
            arriving = []
            drawn = library[7 + turn]
            hand.append(drawn)
            cards_seen += 1
            lands_seen += drawn.kind == "land"

            have = frozenset().union(*(card.colors for card in battlefield + arriving))
            options = [card for card in hand if card.kind == "land"] or [c for c in hand if c.kind == "flex"]
            if options:
                land = max(options, key=lambda card: (len((card.colors & wanted) - have), not card.tapped,
                                                      len(card.colors)))
                hand.remove(land)
                (arriving if land.tapped else battlefield).append(land)
                lands_played += 1
                land_drop_made[turn] += 1

            castable_now = [_payable(cost, pips, battlefield, 0) for _, cost, pips in commanders]
            spent = 0
            progress = True
            while progress:
                progress = False
                for card in sorted((c for c in hand if c.kind in ("rock", "dork", "ramp_spell")),
                                   key=lambda c: c.mana_value):
                    if _payable(card.mana_value, card.pips, battlefield, spent):
                        hand.remove(card)
                        spent += card.mana_value
                        (arriving if card.tapped else battlefield).append(card)
                        progress = True
                        break

            total = sum(card.amount for card in battlefield)
            lands_in_play[turn] += lands_played
            mana_available[turn] += total
            colors_now = frozenset().union(*(card.colors for card in battlefield))
            all_colors[turn] += wanted <= colors_now
            for index, (_, cost, pips) in enumerate(commanders):
                if not castable_on[index] and (castable_now[index] or _payable(cost, pips, battlefield, spent)):
                    castable_on[index] = turn + 1
            if turn == 3:
                stuck_on_two += lands_played <= 2
        flooded += lands_seen / cards_seen >= 0.6
        for index, turn_number in enumerate(castable_on):
            first_castable[index].append(turn_number)

    def rate(count: int) -> float:
        return round(count / trials, 4)

    report_commanders = []
    for (name, cost, _), turns_castable in zip(commanders, first_castable, strict=True):
        made = sorted(t for t in turns_castable if t)
        targets = [t for t in (cost, cost + 1, cost + 2) if 1 <= t <= turns]
        report_commanders.append({
            "name": name, "mana_value": cost,
            "castable_by_turn": {str(t): rate(sum(1 for m in made if m <= t)) for t in targets},
            "median_first_castable_turn": median(made) if len(made) > trials / 2 else None,
        })
    return {
        "policy": list(POLICY), "trials": trials, "turns": turns, "seed": seed,
        "opening_hand": {
            "kept_first_seven": rate(kept_without_mulligan),
            "mulligans": {label: rate(n) for label, n in zip(("0", "1", "2", "3+"), mulligan_counts, strict=True)},
            "lands_in_kept_hand": {str(n): rate(count) for n, count in enumerate(hand_lands) if count},
        },
        "by_turn": [{"turn": t + 1, "average_lands": round(lands_in_play[t] / trials, 2),
                     "made_land_drop": rate(land_drop_made[t]),
                     "average_mana": round(mana_available[t] / trials, 2),
                     "all_colors": rate(all_colors[t])} for t in range(turns)],
        "commanders": report_commanders,
        "stuck_on_two_lands_turn_four": rate(stuck_on_two),
        "flooded": {"rate": rate(flooded), "definition": f"lands are 60% or more of the cards seen by turn {turns}"},
    }
