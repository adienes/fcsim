"""A deck project: one deck's identity, kept across revisions of its list (PLAN.md §12.2).

    decks/<name>/
      deck.toml        a stable id, the display name, the commanders
      current.txt      the list as it stands
      primer.md        accepted piloting guidance; goes into the pilot's prompt
      lessons.jsonl    every observation about piloting this deck, with its evidence and its standing
      CHANGELOG.md     what changed in the primer and the list, and why
      reviews/         one file per review

A pilot starts a game knowing nothing but its list, and plays the deck as a stranger would. What one
game teaches (hold the commander until turn four; attack before casting so Laelia's exile has mana
waiting) was lost with the game. A project keeps it: lessons are recorded with the game and round
they came from, stay tentative until a second game bears them out, and only accepted guidance
reaches the primer. The identity is the directory, not the list: swap ten cards and the deck still
knows how it likes to be played, while a lesson that leans on a card no longer in the list is
flagged rather than taught.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import date
from pathlib import Path

from ..cards import CardDatabase, read_decklist, resolve

PRIMER_WORDS = 600  # a guide, not a cutoff: about 800 tokens, read at the start of every wake's session; a line that earns its place stays


class DeckProject:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not (self.path / "current.txt").is_file():
            raise ValueError(f"{self.path} is not a deck project: it has no current.txt. Make one with `fcsim deck init`.")

    @classmethod
    def init(cls, decklist: str | Path, database: CardDatabase, *, root: str | Path = "decks", name: str | None = None) -> DeckProject:
        """Make a project from a list. The list is copied; the original is left where it is."""
        deck = resolve(read_decklist(decklist), database, online=True)
        if deck.unknown or not deck.commanders:
            raise ValueError(f"{decklist} has unrecognized cards or no commander; run `fcsim deck check` on it first.")
        slug = re.sub(r"[^a-z0-9]+", "-", (name or deck.commanders[0].name.split(",")[0]).lower()).strip("-")
        path = Path(root) / slug
        if (path / "current.txt").exists():
            raise ValueError(f"{path} already exists.")
        (path / "reviews").mkdir(parents=True, exist_ok=True)
        (path / "current.txt").write_text(read_decklist(decklist).to_mtgo(), encoding="utf-8")
        (path / "deck.toml").write_text(f'schema = 1\nid = "{slug}"\nname = "{name or deck.commanders[0].name}"\n'
                                        f'commanders = {json.dumps([c.name for c in deck.commanders])}\n', encoding="utf-8")
        (path / "primer.md").write_text("", encoding="utf-8")
        (path / "lessons.jsonl").write_text("", encoding="utf-8")
        (path / "CHANGELOG.md").write_text(f"# {name or deck.commanders[0].name}\n\n- {date.today()}: project made from `{decklist}`.\n", encoding="utf-8")
        return cls(path)

    @property
    def list_path(self) -> Path:
        return self.path / "current.txt"

    @property
    def deck_id(self) -> str:
        found = re.search(r'^id = "(.+)"$', (self.path / "deck.toml").read_text(encoding="utf-8"), re.M) if (self.path / "deck.toml").is_file() else None
        return found[1] if found else self.path.name

    def primer(self) -> str:
        path = self.path / "primer.md"
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""

    def lessons(self) -> list[dict]:
        path = self.path / "lessons.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.is_file() else []

    def hashes(self, database: CardDatabase) -> dict:
        """What was played, however the file was formatted; and exactly what the pilot was told."""
        deck = resolve(read_decklist(self.list_path), database)
        cards = sorted([f"C:{c.oracle_id}" for c in deck.commanders] + [f"{count}:{c.oracle_id}" for c, count in deck.cards])
        return {"deck_id": self.deck_id, "list_hash": hashlib.sha256("\n".join(cards).encode()).hexdigest()[:16],
                "guidance_hash": hashlib.sha256(self.primer().encode()).hexdigest()[:16]}

    def record(self, lessons: list[dict], *, games: list[str], database: CardDatabase) -> dict:
        """Add what a review observed. A lesson much like one already recorded from other games is the
        same lesson borne out again, and becomes validated; a new one is tentative. Returns counts."""
        deck = resolve(read_decklist(self.list_path), database)
        known, names = self.lessons(), {c.name for c, _ in deck.cards} | {c.name for c in deck.commanders}
        added, confirmed = merge_lessons(known, lessons, games=games, prefix="L")
        for lesson in known:  # a lesson about a card the list no longer runs is kept, and not taught
            lesson["stale"] = bool(lesson["cards"]) and not any(card in names for card in lesson["cards"])
        (self.path / "lessons.jsonl").write_text("".join(json.dumps(k, ensure_ascii=False) + "\n" for k in known), encoding="utf-8")
        return {"added": added, "validated": confirmed, "total": len(known)}

    def log(self, line: str) -> None:
        with (self.path / "CHANGELOG.md").open("a", encoding="utf-8") as out:
            out.write(f"- {date.today()}: {line}\n")


def merge_lessons(known: list[dict], lessons: list[dict], *, games: list[str], prefix: str, deck_id: str | None = None) -> tuple[int, int]:
    """Merge what a review observed into the lessons on record, in place: (added, validated). A lesson the writer says it
    restates (`same_as`), or failing that one much like it in wording, is the same lesson; from a game it was not already
    learned from it is borne out again and becomes validated. `deck_id` records which deck taught it, for lessons shared
    by every deck."""
    added = confirmed = 0
    for lesson in lessons:
        claim = " ".join(str(lesson.get("claim", "")).split())
        if not claim:
            continue
        # Whoever wrote the lesson was shown the ones on record and says which it restates. Likeness of wording is only the
        # fallback: the same lesson learned from another game comes back in other words, and nothing was ever borne out.
        named = next((k for k in known if k["id"] == str(lesson.get("same_as") or "").strip()), None)
        match = named or max(known, key=lambda k: difflib.SequenceMatcher(None, k["claim"].lower(), claim.lower()).ratio(), default=None)
        if match is not None and (named is not None or difflib.SequenceMatcher(None, match["claim"].lower(), claim.lower()).ratio() > 0.6):
            fresh = [g for g in games if g not in match["games"]]
            if fresh:  # the same games reviewed again are not a second witness
                match["games"] += fresh
                match["evidence"].append(str(lesson.get("evidence", "")))
                if deck_id and deck_id not in match.setdefault("decks", []):
                    match["decks"].append(deck_id)
                if match["status"] == "tentative":
                    match["status"], confirmed = "validated", confirmed + 1
            continue
        number = max((int(k["id"][len(prefix):]) for k in known if re.fullmatch(rf"{prefix}\d+", k["id"])), default=0) + 1  # never an id in use
        known.append({"id": f"{prefix}{number:03d}", "claim": claim, "kind": lesson.get("kind", "play"), "status": "tentative",
                      "cards": [c for c in lesson.get("cards", []) if isinstance(c, str)], "evidence": [str(lesson.get("evidence", ""))],
                      "games": list(games), "recorded": str(date.today()), **({"decks": [deck_id]} if deck_id else {})})
        added += 1
    return added, confirmed


class GenericGuide:
    """decks/_generic/: how to play Commander well, whatever the deck. Every pilot reads it before its own deck's primer,
    so what one deck's games teach about mulligans, threats, interaction or attacking is not learned again deck by deck.

        primer.md      the guide itself; begun by hand from what the first games showed, then rewritten as lessons come in
        lessons.jsonl  every general lesson, with the deck and game it came from; validated when another game bears it out
        CHANGELOG.md   when the guide was rewritten, and from what
    """

    def __init__(self, root: str | Path = "decks"):
        self.path = Path(root) / "_generic"

    def primer(self) -> str:
        path = self.path / "primer.md"
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""

    def lessons(self) -> list[dict]:
        path = self.path / "lessons.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.is_file() else []

    def record(self, lessons: list[dict], *, games: list[str], deck_id: str) -> dict:
        known = self.lessons()
        added, confirmed = merge_lessons(known, lessons, games=games, prefix="G", deck_id=deck_id)
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "lessons.jsonl").write_text("".join(json.dumps(k, ensure_ascii=False) + "\n" for k in known), encoding="utf-8")
        return {"added": added, "validated": confirmed, "total": len(known)}

    def log(self, line: str) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        with (self.path / "CHANGELOG.md").open("a", encoding="utf-8") as out:
            out.write(f"- {date.today()}: {line}\n")


def deck_argument(path: str | Path) -> tuple[Path, DeckProject | None]:
    """A deck given on the command line is a list or a project directory: the list to play, and the project if there is one."""
    path = Path(path)
    if path.is_dir():
        project = DeckProject(path)
        return project.list_path, project
    return path, (DeckProject(path.parent) if path.name == "current.txt" and (path.parent / "deck.toml").is_file() else None)
