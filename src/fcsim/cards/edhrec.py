"""EDHREC's average deck for a commander: a representative starting list, not a tuned one.

The endpoint is unofficial and undocumented, so every answer is cached beside the card data and
a failure is reported rather than worked around.
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

from .database import CardsError, default_data_dir

AVERAGE_DECK_URL = "https://json.edhrec.com/pages/average-decks/{slug}.json"
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


def average_deck(commander: str, *, cache_dir: str | Path | None = None, refresh: bool = False) -> str:
    directory = Path(cache_dir) if cache_dir else default_data_dir() / "edhrec"
    cached = directory / f"{slug(commander)}.json"
    if cached.is_file() and not refresh:
        return deck_text(json.loads(cached.read_text(encoding="utf-8")))
    try:
        request = urllib.request.Request(AVERAGE_DECK_URL.format(slug=slug(commander)), headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=30) as response:
            page = json.load(response)
    except urllib.error.HTTPError as error:
        raise CardsError(f"EDHREC has no average deck for {commander!r} (HTTP {error.code}). Check the spelling.") from error
    except (OSError, ValueError) as error:
        raise CardsError(f"Could not reach EDHREC: {error}") from error
    text = deck_text(page)
    directory.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(page), encoding="utf-8")
    return text
