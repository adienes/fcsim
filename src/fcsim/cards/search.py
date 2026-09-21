"""Card research over the local copy of Scryfall's data, in Scryfall's own search syntax.

    o:"cast it from exile" id<=rwb mv<=3 -t:creature

Offline, quick and repeatable, because it reads the same pinned card data the games are played
with; and it speaks the syntax people already know from scryfall.com, so a query can be pasted
either way. Only a useful part of the syntax is understood, and anything else is refused rather
than guessed at: a search that silently ignored a term would mislead whoever trusted its answer.

Understood: bare words and "quoted phrases" (in the name); `name:`; `o:`/`oracle:` (text, or
/regex/); `t:`/`type:`; `kw:`/`keyword:`; `id:`/`identity:`/`ci:` with `:` `<=` `=` `>=` (colours
as letters, `c` for colourless, or a guild, shard or wedge name); `mv`/`cmc`, `pow`, `tou`, `usd`
and `edhrec` with `= < <= > >=`; `produces:`; `is:commander`, `is:land`, `is:permanent`,
`is:gamechanger`; `f:`/`legal:`/`banned:` commander; `-` before any term to negate it; `or`
between terms; `order:edhrec|usd|mv|name` and `direction:desc`. Every search is of cards legal in
Commander unless it says `include:illegal`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .database import CardDatabase, CardsError
from .model import Card

_NAMED = {"colorless": "", "c": "", "white": "w", "blue": "u", "black": "b", "red": "r", "green": "g",
          "azorius": "wu", "dimir": "ub", "rakdos": "br", "gruul": "rg", "selesnya": "gw", "orzhov": "wb", "izzet": "ur", "golgari": "bg",
          "boros": "rw", "simic": "gu", "bant": "gwu", "esper": "wub", "grixis": "ubr", "jund": "brg", "naya": "rgw", "abzan": "wbg",
          "jeskai": "urw", "sultai": "bgu", "mardu": "rwb", "temur": "gur", "fivecolor": "wubrg", "wubrg": "wubrg"}
_TERM = re.compile(r"^(?P<neg>-?)(?P<key>[a-z]+)(?P<op><=|>=|!=|=|:|<|>)(?P<value>.+)$", re.I)
_WORDS = re.compile(r'-?[a-z]+(?:<=|>=|!=|=|:|<|>)(?:/(?:[^/\\]|\\.)*/|"[^"]*"|\S+)|"[^"]*"|\S+', re.I)
_COMPARE = {"=": lambda a, b: a == b, ":": lambda a, b: a == b, "!=": lambda a, b: a != b, "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}


def _colors(value: str) -> frozenset[str]:
    value = value.lower()
    letters = _NAMED.get(value, value)
    if not set(letters) <= set("wubrg"):
        raise CardsError(f"{value!r} is not a set of colours: use letters (rwb), c for colourless, or a name such as mardu.")
    return frozenset(letters.upper())


def _number(card: Card, key: str) -> float | None:
    face = card.front
    raw = {"mv": card.mana_value, "cmc": card.mana_value, "usd": card.price_usd, "edhrec": card.edhrec_rank,
           "pow": face.power, "power": face.power, "tou": face.toughness, "toughness": face.toughness}[key]
    try:
        return None if raw is None else float(raw)
    except ValueError:
        return None  # a printed "*"


def _term(text: str):
    """One search term as a test on a card."""
    found = _TERM.match(text)
    if not found:
        words = text.lstrip("-").lower()
        return (lambda card: words not in card.name.lower()) if text.startswith("-") else (lambda card: words in card.name.lower())
    negate, key, op, value = found["neg"] == "-", found["key"].lower(), found["op"], found["value"]
    if key in ("o", "oracle", "name", "t", "type", "kw", "keyword"):
        if op != ":":
            raise CardsError(f"`{key}` takes `:`, as in {key}:flying.")
        field = {"o": lambda c: c.text, "oracle": lambda c: c.text, "name": lambda c: c.name, "t": lambda c: " // ".join(f.type_line for f in c.faces),
                 "type": lambda c: " // ".join(f.type_line for f in c.faces), "kw": lambda c: ", ".join(c.keywords),
                 "keyword": lambda c: ", ".join(c.keywords)}[key]
        if len(value) > 2 and value.startswith("/") and value.endswith("/"):
            pattern = re.compile(value[1:-1], re.I)
            test = lambda card: bool(pattern.search(field(card)))  # noqa: E731
        else:
            test = lambda card: value.lower() in field(card).lower().replace("~", card.name.lower())  # noqa: E731
    elif key in ("id", "identity", "ci", "c", "color", "commander"):
        wanted = _colors(value)
        relation = {":": "<=", "=": "=", "<=": "<=", ">=": ">=", "<": "<", ">": ">", "!=": "!="}[op]
        test = lambda card: {"<=": frozenset(card.color_identity) <= wanted, "=": frozenset(card.color_identity) == wanted,  # noqa: E731
                             ">=": frozenset(card.color_identity) >= wanted, "<": frozenset(card.color_identity) < wanted,
                             ">": frozenset(card.color_identity) > wanted, "!=": frozenset(card.color_identity) != wanted}[relation]
    elif key in ("mv", "cmc", "pow", "power", "tou", "toughness", "usd", "edhrec"):
        try:
            number = float(value)
        except ValueError:
            raise CardsError(f"`{key}` compares with a number, as in {key}<=3.") from None
        test = lambda card: (held := _number(card, key)) is not None and _COMPARE[op](held, number)  # noqa: E731
    elif key == "produces":
        wanted = _colors(value) or frozenset("C")
        test = lambda card: wanted <= frozenset(card.produced_mana)  # noqa: E731
    elif key == "is":
        kinds = {"commander": lambda c: "Legendary" in c.front.type_line and ("Creature" in c.front.type_line or "can be your commander" in c.text),
                 "land": lambda c: c.is_land, "permanent": lambda c: c.is_permanent, "gamechanger": lambda c: c.game_changer,
                 "spell": lambda c: not c.is_land}
        if value.lower() not in kinds:
            raise CardsError(f"`is:` knows {', '.join(kinds)}.")
        test = kinds[value.lower()]
    elif key in ("f", "format", "legal", "banned"):
        if value.lower() not in ("commander", "edh"):
            raise CardsError("Only Commander legality is kept locally.")
        test = (lambda card: card.legality == "banned") if key == "banned" else (lambda card: card.legality == "legal")
    else:
        raise CardsError(f"`{key}:` is not a search term this understands. See `fcsim cards search --help`.")
    return (lambda card: not test(card)) if negate else test


def search(query: str, database: CardDatabase | None = None, *, limit: int = 25) -> dict:
    """Cards matching a Scryfall-style query, most played in Commander first unless the query orders them otherwise."""
    database = database or CardDatabase()
    # A term is key, operator and a value that may be a /regex with spaces/ or a "quoted phrase"; or a bare word or phrase.
    # No grouping with parentheses: `or` splits the whole query.
    words = [word.replace('"', "") for word in _WORDS.findall(query)]
    order, descending, legal_only, groups, current = "edhrec", False, True, [], []
    for word in words:
        lowered = word.lower()
        if lowered == "or":
            groups.append(current)
            current = []
        elif lowered.startswith("order:"):
            order = lowered.split(":", 1)[1]
            if order not in ("edhrec", "usd", "mv", "cmc", "name"):
                raise CardsError("`order:` is one of edhrec, usd, mv, name.")
        elif lowered.startswith("direction:"):
            descending = lowered.endswith("desc")
        elif lowered == "include:illegal":
            legal_only = False
        elif lowered != "and":
            current.append(_term(word))
    groups.append(current)
    groups = [g for g in groups if g]
    if not groups:
        raise CardsError("An empty search would list every card.")
    connection = sqlite3.connect(Path(database.path))
    try:
        cards = [Card.from_dict(json.loads(raw)) for (raw,) in connection.execute("select data from cards")]
    finally:
        connection.close()
    found = [card for card in cards if (card.legality == "legal" or not legal_only) and any(all(test(card) for test in group) for group in groups)]
    key = {"edhrec": lambda c: c.edhrec_rank if c.edhrec_rank is not None else 10 ** 9, "usd": lambda c: c.price_usd if c.price_usd is not None else 10 ** 9,
           "mv": lambda c: c.mana_value, "cmc": lambda c: c.mana_value, "name": lambda c: c.name}[order]
    found.sort(key=lambda c: (key(c), c.name), reverse=descending)
    return {"query": query, "total": len(found), "card_data": database.info().get("scryfall_updated_at", ""),
            "cards": [{"name": c.name, "mana_cost": c.mana_cost, "mana_value": c.mana_value, "type_line": " // ".join(f.type_line for f in c.faces),
                       "text": c.text, "color_identity": "".join(c.color_identity), "edhrec_rank": c.edhrec_rank, "price_usd": c.price_usd,
                       "game_changer": c.game_changer, **({"power": c.front.power, "toughness": c.front.toughness} if c.front.power is not None else {})}
                      for c in found[:limit]]}


def render(result: dict, *, text: bool = True) -> str:
    lines = [f"{result['total']} card(s) for `{result['query']}`" + (f", the first {len(result['cards'])} shown" if result["total"] > len(result["cards"]) else "")
             + f"  (card data {result['card_data'][:10]})"]
    for card in result["cards"]:
        stats = f" {card['power']}/{card['toughness']}" if "power" in card else ""
        extra = ", ".join(x for x in (f"EDHREC #{card['edhrec_rank']}" if card["edhrec_rank"] else "", f"${card['price_usd']:.2f}" if card["price_usd"] else "",
                                      "GAME CHANGER" if card["game_changer"] else "") if x)
        lines.append(f"\n{card['name']}  {card['mana_cost']}  [{card['type_line']}{stats}]  ({extra})")
        if text:
            lines += [f"    {line}" for line in card["text"].splitlines()]
    return "\n".join(lines)
