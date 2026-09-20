"""A local SQLite index of Scryfall's Oracle card data. Only `update` touches the network."""

from __future__ import annotations

import difflib
import gzip
import json
import os
import re
import sqlite3
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .model import Card

BULK_DATA_URL = "https://api.scryfall.com/bulk-data/oracle-cards"
RULINGS_URL = "https://api.scryfall.com/bulk-data/rulings"
NAMED_URL = "https://api.scryfall.com/cards/named?fuzzy="
_HEADERS = {"User-Agent": "fcsim/0.1 (Commander playtesting)", "Accept": "application/json"}
# Objects that share names with real cards but can never be in a deck.
_SKIPPED_LAYOUTS = frozenset("token double_faced_token emblem art_series planar scheme vanguard".split())
_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE cards (oracle_id TEXT PRIMARY KEY, name TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE names (norm TEXT NOT NULL, oracle_id TEXT NOT NULL, rank INTEGER NOT NULL);
CREATE INDEX names_norm ON names (norm, rank);
CREATE TABLE rulings (oracle_id TEXT NOT NULL, published_at TEXT NOT NULL, comment TEXT NOT NULL);
CREATE INDEX rulings_card ON rulings (oracle_id);
"""


class CardsError(ValueError):
    """A card data problem that can be reported to a user or an agent."""


def normalize_name(name: str) -> str:
    """Fold case, accents, quote styles and split-card separators, so exports from any site match."""
    text = unicodedata.normalize("NFKD", name.replace("Æ", "Ae").replace("æ", "ae"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"\s*/{1,3}\s*", " // ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def default_data_dir() -> Path:
    """`FCSIM_DATA`, else `data/` in a source checkout, else the user's cache directory."""
    if override := os.environ.get("FCSIM_DATA"):
        return Path(override).expanduser()
    checkout = Path(__file__).resolve().parents[3]
    if (checkout / "pyproject.toml").is_file():
        return checkout / "data"
    return Path.home() / ".cache" / "fcsim"


def _card_objects(path: Path) -> Iterator[dict]:
    """Read Scryfall bulk data as JSON Lines, or as its one-card-per-line JSON array."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as lines:
        for line in lines:
            line = line.strip().rstrip(",")
            if line.startswith("{"):
                yield json.loads(line)


class CardDatabase:
    """Exact and face-name lookup of cards by the names decklists use."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_data_dir() / "scryfall" / "cards.sqlite"
        if not self.path.is_file():
            raise CardsError(f"No card database at {self.path}. Run `fcsim cards update` first.")
        # Read-only, and looked up from the tool server's thread as well as the driver's, never at once.
        self._db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        self._cache: dict[str, Card | None] = {}
        self._norms: list[str] | None = None

    def close(self) -> None:
        self._db.close()

    def info(self) -> dict:
        meta = dict(self._db.execute("SELECT key, value FROM meta"))
        built = datetime.fromisoformat(meta["built_at"])
        return {"path": str(self.path), "cards": int(meta["cards"]), "source": meta["source"],
                "scryfall_updated_at": meta["updated_at"], "built_at": meta["built_at"],
                "age_days": (datetime.now(UTC) - built).days}

    def find(self, name: str) -> Card | None:
        """The card with this name, or with a face of this name; None when there is none."""
        norm = normalize_name(name)
        if norm not in self._cache:
            row = self._db.execute(
                "SELECT data FROM names JOIN cards USING (oracle_id) WHERE norm = ? ORDER BY rank LIMIT 1",
                (norm,)).fetchone()
            self._cache[norm] = Card.from_dict(json.loads(row[0])) if row else None
        return self._cache[norm]

    def get(self, name: str) -> Card:
        card = self.find(name)
        if card is None:
            hint = ", ".join(self.suggest(name))
            raise CardsError(f"Unknown card {name!r}." + (f" Did you mean: {hint}?" if hint else ""))
        return card

    def alias(self, name: str, *, online: bool = False) -> Card | None:
        """A card known by another name on some printing, such as "Bayo, Irritable Instructor" for
        Electro, Assaulting Battery. The Oracle data holds only Oracle names, so with `online` an
        unfamiliar name is asked of Scryfall once and remembered beside the database. A name close
        to a real card's is left alone: that is a typo to report, not an alias to guess."""
        path = self.path.with_name("aliases.json")
        known = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        norm = normalize_name(name)
        if norm not in known and online and not self.suggest(name):
            try:
                request = urllib.request.Request(NAMED_URL + urllib.parse.quote(name), headers=_HEADERS)
                with urllib.request.urlopen(request, timeout=20) as response:
                    known[norm] = json.load(response).get("name")
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    return None
                known[norm] = None  # Scryfall does not know it either; do not ask again
            except (OSError, ValueError):
                return None
            path.write_text(json.dumps(known, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return self.find(known[norm]) if known.get(norm) else None

    def rulings(self, card: Card) -> list[dict]:
        """Official rulings for a card, oldest first. Empty if the database predates rulings."""
        try:
            rows = self._db.execute("SELECT published_at, comment FROM rulings WHERE oracle_id = ? "
                                    "ORDER BY published_at, rowid", (card.oracle_id,)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{"date": date, "text": text} for date, text in rows]

    def suggest(self, name: str, limit: int = 3) -> list[str]:
        """Real card names close to a misspelled one."""
        if self._norms is None:
            self._norms = [row[0] for row in self._db.execute("SELECT DISTINCT norm FROM names WHERE rank < 2")]
        close = difflib.get_close_matches(normalize_name(name), self._norms, n=limit, cutoff=0.75)
        return [card.name for norm in close if (card := self.find(norm))]

    @staticmethod
    def build(objects: Iterable[dict], path: str | Path, *, source: str, updated_at: str,
              rulings: Iterable[dict] = ()) -> int:
        """Write a fresh database, replacing any old one only once the new one is complete."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".partial")
        partial.unlink(missing_ok=True)
        count = 0
        with closing(sqlite3.connect(partial)) as db, db:
            db.executescript(_SCHEMA)
            for obj in objects:
                if obj.get("layout") in _SKIPPED_LAYOUTS or "oracle_id" not in obj:
                    continue
                card = Card.from_scryfall(obj)
                db.execute("INSERT OR IGNORE INTO cards VALUES (?, ?, ?)",
                           (card.oracle_id, card.name, json.dumps(card.to_dict(), ensure_ascii=False)))
                # A full name beats a face name, and a deck-legal card beats a same-named oddity.
                names = [(card.name, 0 if card.legality != "not_legal" else 1)]
                if len(card.faces) > 1:
                    names += [(face.name, 2 + min(i, 1)) for i, face in enumerate(card.faces)]
                db.executemany("INSERT INTO names VALUES (?, ?, ?)",
                               [(normalize_name(n), card.oracle_id, rank) for n, rank in names])
                count += 1
            if not count:
                raise CardsError("The card data contained no cards; keeping the existing database.")
            db.executemany("INSERT INTO rulings VALUES (?, ?, ?)",
                           ((r["oracle_id"], r.get("published_at", ""), r["comment"]) for r in rulings
                            if r.get("oracle_id") and r.get("comment")))
            db.executemany("INSERT INTO meta VALUES (?, ?)", {
                "cards": str(count), "source": source, "updated_at": updated_at,
                "built_at": datetime.now(UTC).isoformat(timespec="seconds")}.items())
        partial.replace(path)
        return count

    @classmethod
    def update(cls, data_dir: str | Path | None = None) -> dict:
        """Download Scryfall's current Oracle cards file and rebuild the index from it."""
        directory = Path(data_dir) if data_dir else default_data_dir() / "scryfall"
        directory.mkdir(parents=True, exist_ok=True)

        def download(listing_url: str, stem: str) -> tuple[Path, dict]:
            with urllib.request.urlopen(urllib.request.Request(listing_url, headers=_HEADERS), timeout=30) as r:
                bulk = json.load(r)
            url = bulk.get("jsonl_download_uri") or bulk["download_uri"]
            target = directory / (f"{stem}.jsonl.gz" if url.endswith(".gz") else f"{stem}.json")
            partial = target.with_suffix(target.suffix + ".partial")
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=120) as r, \
                    partial.open("wb") as out:
                while chunk := r.read(1 << 20):
                    out.write(chunk)
            partial.replace(target)
            return target, {**bulk, "url": url}

        try:
            cards, bulk = download(BULK_DATA_URL, "oracle-cards")
            rulings, _ = download(RULINGS_URL, "rulings")
        except (OSError, KeyError, ValueError) as exc:
            raise CardsError(f"Could not download Scryfall card data: {exc}") from exc
        cls.build(_card_objects(cards), directory / "cards.sqlite", source=bulk["url"],
                  updated_at=bulk.get("updated_at", ""), rulings=_card_objects(rulings))
        with closing(cls(directory / "cards.sqlite")) as database:
            return database.info()
