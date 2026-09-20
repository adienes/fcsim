"""Parse MTGO-style decklists, tolerating what Moxfield, Archidekt and Arena exports add."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .database import CardsError, normalize_name

_LINE = re.compile(r"^(?:(?P<count>\d+)\s*[xX]?\s+)?(?P<name>\S.*)$")
_HEADER = re.compile(r"^(?:(?://|#)\s*)?(?P<title>[A-Za-z ]+?)\s*(?:\(\d+\))?\s*:?$")
_SECTIONS = {
    "commander": "commander", "commanders": "commander", "command zone": "commander",
    "deck": "main", "main": "main", "mainboard": "main", "main deck": "main", "maindeck": "main",
    "sideboard": "sideboard", "companion": "companion",
    "maybeboard": None, "considering": None, "tokens": None, "token": None, "about": None,
}
# Printing details and site tags that follow a card name, removed until none remain.
_TRAILERS = [re.compile(pattern) for pattern in (
    r"\s+\*[A-Za-z]+\*$",                         # *F* foil, *E* etched
    r"\s+\^[^^]*\^$",                             # ^Tag,#color^ (Archidekt)
    r"\s+\[[^\]]*\]$",                            # [Category] (Archidekt)
    r"\s+#\S.*$",                                 # #tags (Moxfield)
    r"\s+\([A-Za-z0-9]{2,6}\)(?:\s+[\w★†-]+)?$",  # (SET) 123
)]


@dataclass(frozen=True)
class Entry:
    count: int
    name: str
    line: int


@dataclass(frozen=True)
class Decklist:
    commanders: tuple[Entry, ...] = ()
    main: tuple[Entry, ...] = ()
    sideboard: tuple[Entry, ...] = ()
    companion: tuple[Entry, ...] = ()
    notes: tuple[str, ...] = field(default=(), compare=False)

    def to_mtgo(self) -> str:
        """The canonical form: the main deck, a blank line, then the commanders."""
        lines = [f"{e.count} {e.name}" for e in self.main] + [""]
        return "\n".join(lines + [f"{e.count} {e.name}" for e in self.commanders]) + "\n"


def _clean_name(name: str) -> str:
    changed = True
    while changed:
        changed = False
        for trailer in _TRAILERS:
            name, n = trailer.subn("", name)
            changed = changed or bool(n)
    return name.strip()


def _merge(entries: list[Entry]) -> tuple[Entry, ...]:
    merged: dict[str, Entry] = {}
    for entry in entries:
        key = normalize_name(entry.name)
        merged[key] = replace(merged[key], count=merged[key].count + entry.count) if key in merged else entry
    return tuple(merged.values())


def parse_decklist(text: str, *, commanders: list[str] | tuple[str, ...] = ()) -> Decklist:
    """Parse a decklist. `commanders` names the commanders when the file does not mark them.

    Without headers, MTGO's convention applies: the block after the blank line is the
    sideboard, and a sideboard of one or two cards is the command zone.
    """
    blocks: list[tuple[str | None, list[Entry]]] = [("", [])]  # (section or "" if unlabeled, cards)
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            if blocks[-1][1]:
                blocks.append(("", []))
            continue
        if line.lower().startswith("name ") and blocks[-1][0] is None:
            continue  # Arena's "About / Name <deck>" preamble
        header = _HEADER.match(line)
        if header and header["title"].lower() in _SECTIONS:
            if not blocks[-1][1]:
                blocks.pop()
            blocks.append((_SECTIONS[header["title"].lower()], []))
            continue
        sideboard_prefix = re.match(r"^SB:\s*", line, flags=re.IGNORECASE)
        match = _LINE.match(line[sideboard_prefix.end():] if sideboard_prefix else line)
        name = _clean_name(match["name"]) if match else ""
        if not name or re.fullmatch(r"\d+\s*[xX]?", name):  # a count with no card after it
            raise CardsError(f"Line {number}: cannot read {raw!r} as a card.")
        entry = Entry(int(match["count"] or 1), name, number)
        if sideboard_prefix and blocks[-1][0] != "sideboard":
            blocks.append(("sideboard", []))
        blocks[-1][1].append(entry)
    blocks = [(section, cards) for section, cards in blocks if cards and section is not None]

    sections: dict[str, list[Entry]] = {"commander": [], "main": [], "sideboard": [], "companion": []}
    notes: list[str] = []
    unlabeled = [cards for section, cards in blocks if section == ""]
    for section, cards in blocks:
        if section:
            sections[section] += cards
    if len(unlabeled) >= 2 and not sections["commander"] and not sections["sideboard"]:
        # MTGO puts the command zone last, after a blank line; some sites put it first.
        for index, place in ((-1, "final"), (0, "first")):
            if sum(e.count for e in unlabeled[index]) <= 2:
                sections["commander"] += unlabeled.pop(index)
                notes.append(f"Read the {place} block of one or two cards as the command zone.")
                break
    for block in unlabeled:
        sections["main"] += block
    if not sections["commander"] and 1 <= sum(e.count for e in sections["sideboard"]) <= 2:
        sections["commander"], sections["sideboard"] = sections["sideboard"], []
        notes.append("Read the one- or two-card sideboard as the command zone (MTGO convention).")

    for wanted in commanders:
        key = normalize_name(wanted)
        for source in ("main", "sideboard", "commander"):
            found = [e for e in sections[source] if normalize_name(e.name) == key]
            if found:
                if source != "commander":
                    sections[source].remove(found[0])
                    if found[0].count > 1:
                        sections[source].append(replace(found[0], count=found[0].count - 1))
                    sections["commander"].append(replace(found[0], count=1))
                break
        else:
            sections["commander"].append(Entry(1, wanted, 0))
            notes.append(f"{wanted} was not in the list, so it was added as a commander.")
    if commanders:
        keep = {normalize_name(name) for name in commanders}
        demoted = [e for e in sections["commander"] if normalize_name(e.name) not in keep]
        sections["commander"] = [e for e in sections["commander"] if normalize_name(e.name) in keep]
        sections["main"] += demoted

    return Decklist(commanders=_merge(sections["commander"]), main=_merge(sections["main"]),
                    sideboard=_merge(sections["sideboard"]), companion=_merge(sections["companion"]),
                    notes=tuple(notes))


def read_decklist(path: str | Path, *, commanders: list[str] | tuple[str, ...] = ()) -> Decklist:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise CardsError(f"Cannot read decklist {path}: {exc}") from exc
    return parse_decklist(text, commanders=commanders)
