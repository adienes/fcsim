"""EDHREC's average deck for a commander: a representative starting list, not a tuned one.

The endpoint is unofficial and undocumented, so every answer is cached beside the card data and
a failure is reported rather than worked around.
"""

from __future__ import annotations

import json
import random
import re
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

from .database import CardsError, default_data_dir

AVERAGE_DECK_URL = "https://json.edhrec.com/pages/average-decks/{slug}.json"
POPULAR_URL = "https://json.edhrec.com/pages/commanders/{window}.json"  # week, month or year (the past two years)
_HEADERS = {"User-Agent": "fcsim/0.1 (Commander playtesting)", "Accept": "application/json"}


def slug(commander: str) -> str:
    text = unicodedata.normalize("NFKD", commander).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text.replace("'", "")).strip("-")


def deck_text(page: dict) -> str:
    """MTGO text from an EDHREC average-deck page: the 99, a blank line, the commander(s)."""
    deck = page.get("deck") or {}
    cards = [(name, count) for group in (deck.get("cards") or {}).values() for name, count in group]
    commanders = deck.get("commander") or []
    if not cards or not commanders:
        raise CardsError("EDHREC's answer holds no deck; its format may have changed.")
    lines = [f"{count} {name}" for name, count in sorted(cards)]
    return "\n".join(lines) + "\n\n" + "\n".join(f"1 {name}" for name in commanders) + "\n"


def _page(url: str, cached: Path, refresh: bool, missing: str) -> dict:
    if cached.is_file() and not refresh:
        return json.loads(cached.read_text(encoding="utf-8"))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=30) as response:
            page = json.load(response)
    except urllib.error.HTTPError as error:
        raise CardsError(f"{missing} (HTTP {error.code}).") from error
    except (OSError, ValueError) as error:
        raise CardsError(f"Could not reach EDHREC: {error}") from error
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(page), encoding="utf-8")
    return page


def average_deck(commander: str, *, cache_dir: str | Path | None = None, refresh: bool = False, page_slug: str | None = None) -> str:
    """`page_slug` is EDHREC's own name for the page, where a list of commanders gave one: it differs
    from the card's name for two-faced commanders."""
    directory = Path(cache_dir) if cache_dir else default_data_dir() / "edhrec"
    name = page_slug or slug(commander)
    return deck_text(_page(AVERAGE_DECK_URL.format(slug=name), directory / f"{name}.json", refresh,
                           f"EDHREC has no average deck for {commander!r}. Check the spelling"))


def popular_commanders(*, window: str = "year", cache_dir: str | Path | None = None, refresh: bool = False) -> list[dict]:
    """EDHREC's hundred most-built commanders over `window`, most popular first: {name, slug, decks}."""
    directory = Path(cache_dir) if cache_dir else default_data_dir() / "edhrec"
    page = _page(POPULAR_URL.format(window=window), directory / f"popular-{window}.json", refresh, "EDHREC has no such list of commanders")
    try:
        views = page["container"]["json_dict"]["cardlists"][0]["cardviews"]
        return [{"name": v["name"], "slug": v["sanitized"], "decks": v.get("num_decks")} for v in views]
    except (KeyError, IndexError, TypeError) as error:
        raise CardsError("EDHREC's list of commanders has changed its format.") from error


def random_commanders(count: int, *, seed: int | None = None, exclude: tuple[str, ...] = (), window: str = "year",
                      cache_dir: str | Path | None = None) -> list[dict]:
    """`count` different commanders drawn evenly from the popular list, none of them in `exclude`."""
    skip = {slug(name) for name in exclude}
    pool = [c for c in popular_commanders(window=window, cache_dir=cache_dir) if slug(c["name"]) not in skip and c["slug"] not in skip]
    return random.Random(seed).sample(pool, count)
