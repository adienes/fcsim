"""Read immutable rules snapshots and return bounded, source-attributed results."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


class RulesError(ValueError):
    """A reference or lookup problem that can be reported to an agent."""


_RULE = re.compile(r"^(\d{3}\.\d+[a-z]?)\.?[ \t]+\S")
_SECTION = re.compile(r"^(\d{3})\. (.+)$")
_NUMBER = re.compile(r"\d{3}(?:\.\d+[a-z]?)?")
_STOP_WORDS = frozenset("a an and are as at be by can do does for from how i in is it of on or the to what when with".split())


@dataclass(frozen=True)
class Entry:
    key: str
    kind: str
    text: str
    section: str
    line_start: int
    line_end: int


def _number(value: str) -> str:
    value = re.sub(r"^rule\s+", "", value.strip(), flags=re.IGNORECASE).rstrip(".").lower()
    if not _NUMBER.fullmatch(value):
        raise RulesError(f"Invalid rule number: {value!r}. Use e.g. 117, 117.3, or 117.3c.")
    return value


def _page(offset: int, limit: int) -> None:
    if not isinstance(offset, int) or offset < 0:
        raise RulesError("offset must be a nonnegative integer.")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise RulesError("limit must be between 1 and 100.")


class RulesReference:
    """A pinned reference, with no file or network access exposed to players.

    The default version is explicit in the manifest, never selected by today's
    date. All results carry source hashes and version information. Methods return
    JSON-compatible dictionaries for a future MCP/API tool registry.
    """

    def __init__(self, directory: str | Path | None = None, *, version: str | None = None,
                 as_of: date | None = None, allow_future: bool = False):
        if directory is None:
            bundled = Path(__file__).with_name("_data")
            directory = bundled if bundled.is_dir() else Path(__file__).resolve().parents[3] / "rules"
        self.directory = Path(directory).resolve()
        try:
            manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
            if manifest["schema_version"] != 1:
                raise RulesError("Unsupported rules manifest schema.")
            selected = version or manifest["default_version"]
            matches = [s for s in manifest["snapshots"] if s["version"] == selected]
            if len(matches) != 1:
                raise RulesError(f"Expected exactly one snapshot for version {selected!r}.")
            self.snapshot = dict(matches[0])
            effective_date = date.fromisoformat(self.snapshot["effective_date"])
            self.as_of = as_of or date.today()
            if effective_date > self.as_of and not allow_future:
                raise RulesError(f"Rules {selected} take effect on {effective_date}, after {self.as_of}. "
                                 "Select an effective version or explicitly allow future rules.")
            self.is_effective = effective_date <= self.as_of
            raw = self._verified_bytes("text")
            self._verified_bytes("source")
            text = raw.decode(self.snapshot["text_encoding"])
        except (OSError, KeyError, TypeError, UnicodeError, ValueError) as exc:
            if isinstance(exc, RulesError):
                raise
            raise RulesError(f"Cannot load rules snapshot: {exc}") from exc
        found_date = re.search(r"These rules are effective as of ([A-Za-z]+ \d{1,2}, \d{4})\.", text)
        if not found_date or datetime.strptime(found_date[1], "%B %d, %Y").date() != effective_date:
            raise RulesError("The text's effective date does not match its manifest.")
        self.sections: dict[str, str] = {"100": "General"}
        self.rules: dict[str, Entry] = {}
        self.glossary: dict[str, Entry] = {}
        self._parse(text)

    def _path(self, relative: str) -> Path:
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise RulesError("Snapshot paths must be relative, without '..' components.")
        path = (self.directory / relative).resolve()
        if not path.is_relative_to(self.directory):
            raise RulesError("Snapshot paths must stay inside the rules directory.")
        return path

    def _verified_bytes(self, kind: str) -> bytes:
        raw = self._path(self.snapshot[f"{kind}_path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.snapshot[f"{kind}_sha256"]:
            raise RulesError(f"SHA-256 mismatch for {kind} snapshot; restore the pinned file.")
        return raw

    def _parse(self, text: str) -> None:
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith("100.1. ")), None)
        if start is None:
            raise RulesError("No numbered rules body found.")
        chapters = {line.strip() for line in lines[:start] if re.fullmatch(r"[1-9]\. .+", line.strip())}
        current: tuple[str, int] | None = None
        section = "100"

        def finish(end: int) -> None:
            if current is None:
                return
            key, begin = current
            while end > begin and not lines[end - 1].strip():
                end -= 1
            if key in self.rules:
                raise RulesError(f"Duplicate rule {key}; check the text extraction.")
            self.rules[key] = Entry(key, "rule", "\n".join(lines[begin:end]),
                                    key[:3], begin + 1, end)

        glossary_start = None
        for i in range(start, len(lines)):
            line = lines[i].strip()
            if line == "Glossary":
                finish(i)
                glossary_start = i + 1
                break
            heading = _SECTION.fullmatch(line)
            rule = _RULE.match(line)
            if heading:
                finish(i)
                current = None
                section, title = heading.groups()
                self.sections[section] = title
            elif rule:
                finish(i)
                key = rule[1]
                if key[:3] != section:
                    raise RulesError(f"Rule {key} appears outside its section; check the extraction.")
                current = (key, i)
            elif line in chapters:
                # Chapter headings belong to neither neighboring rule.
                finish(i)
                current = None
        if glossary_start is None:
            raise RulesError("No glossary boundary found; the snapshot may be incomplete.")
        end = next((i for i in range(glossary_start, len(lines)) if lines[i].strip() == "Credits"), None)
        if end is None:
            raise RulesError("No credits boundary found; the snapshot may be incomplete.")
        i = glossary_start
        while i < end:
            if not lines[i].strip():
                i += 1
                continue
            begin = i
            while i < end and lines[i].strip():
                i += 1
            term = lines[begin].strip()
            if i - begin < 2 or term.casefold() in self.glossary:
                raise RulesError(f"Malformed or duplicate glossary entry: {term!r}.")
            self.glossary[term.casefold()] = Entry(term, "glossary", "\n".join(lines[begin:i]),
                                                  "glossary", begin + 1, i)

    def _metadata(self) -> dict:
        return {**self.snapshot, "as_of": self.as_of.isoformat(), "is_effective": self.is_effective}

    def _entry(self, entry: Entry) -> dict:
        references = re.findall(r"\brules?\s+(\d{3}(?:\.\d+[a-z]?)?)", entry.text, flags=re.IGNORECASE)
        result = {"kind": entry.kind, "id": entry.key, "text": entry.text,
                  "section": {"id": entry.section, "title": self.sections.get(entry.section, "Glossary")},
                  "line_start": entry.line_start, "line_end": entry.line_end,
                  "references": list(dict.fromkeys(references))}
        if entry.kind == "rule":
            parent = self.rules.get(re.sub(r"[a-z]+$", "", entry.key)) if entry.key[-1].isalpha() else None
            result["parent"] = {"id": parent.key, "text": parent.text} if parent else None
            result["children"] = [n for n in self.rules if re.sub(r"[a-z]+$", "", n) == entry.key and n != entry.key]
        return result

    def info(self) -> dict:
        return {"snapshot": self._metadata(), "rule_count": len(self.rules),
                "section_count": len(self.sections), "glossary_count": len(self.glossary)}

    def get_rule(self, number: str) -> dict:
        key = _number(number)
        if key not in self.rules:
            raise RulesError(f"Rule {key} was not found in {self.snapshot['version']}. "
                             "Use get_section for a three-digit section, or search_rules for text.")
        return {"snapshot": self._metadata(), "rule": self._entry(self.rules[key])}

    def get_section(self, number: str, *, offset: int = 0, limit: int = 20) -> dict:
        _page(offset, limit)
        key = _number(number)
        if key not in self.sections:
            raise RulesError(f"Section {key} was not found. Use a three-digit section such as 117.")
        entries = [e for e in self.rules.values() if e.section == key]
        stop = offset + limit
        return {"snapshot": self._metadata(), "section": {"id": key, "title": self.sections[key]},
                "total": len(entries), "offset": offset,
                "next_offset": stop if stop < len(entries) else None,
                "rules": [self._entry(e) for e in entries[offset:stop]]}

    def get_glossary(self, term: str) -> dict:
        entry = self.glossary.get(term.strip().casefold())
        if entry is None:
            raise RulesError(f"Glossary term {term!r} was not found; try search_rules.")
        return {"snapshot": self._metadata(), "entry": self._entry(entry)}

    def search_rules(self, query: str, *, limit: int = 10) -> dict:
        _page(0, limit)
        if not query.strip() or len(query) > 1000:
            raise RulesError("A search query must contain 1–1000 characters.")
        tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", query.casefold())))
        tokens = [t for t in tokens if t not in _STOP_WORDS]
        if not tokens:
            raise RulesError("Include a rule topic, such as 'replacement effects' or 'commander'.")
        # Quote each token: agent input is ordinary text, never FTS or SQL syntax.
        match = " OR ".join('"' + t + '"' for t in tokens)
        entries = [*self.rules.values(), *self.glossary.values()]
        # The small derived index is disposable; no database is shipped or trusted.
        # A connection per query also permits callers on different worker threads.
        try:
            with closing(sqlite3.connect(":memory:")) as connection:
                connection.execute("CREATE VIRTUAL TABLE entries USING fts5(heading, text, tokenize='porter unicode61')")
                connection.executemany("INSERT INTO entries(rowid, heading, text) VALUES (?, ?, ?)",
                    ((i + 1, self.sections.get(e.section, e.key), e.text) for i, e in enumerate(entries)))
                rows = connection.execute("SELECT rowid FROM entries WHERE entries MATCH ? "
                                          "ORDER BY bm25(entries, 4.0, 1.0), rowid LIMIT ?", (match, limit)).fetchall()
        except sqlite3.OperationalError as exc:
            raise RulesError(f"Rules search needs a Python SQLite build with FTS5 support: {exc}") from exc
        return {"snapshot": self._metadata(), "query": query,
                "results": [self._entry(entries[row[0] - 1]) for row in rows]}

    def lookup_rule(self, query: str, *, limit: int = 10) -> dict:
        """The plan's single read tool: dispatch a number, glossary term, or query."""
        try:
            key = _number(query)
        except RulesError:
            if query.strip().casefold() in self.glossary:
                return self.get_glossary(query)
            return self.search_rules(query, limit=limit)
        return self.get_section(key, limit=limit) if "." not in key else self.get_rule(key)

    def export_snapshot(self, destination: str | Path) -> dict:
        """Host-only operation: export a self-contained pin into a NEW directory."""
        # Recheck bytes at export time, even if the store has been alive for hours.
        source, text = self._verified_bytes("source"), self._verified_bytes("text")
        target = Path(destination)
        target.mkdir(parents=True, exist_ok=False)
        for kind, raw in (("source", source), ("text", text)):
            path = target / self.snapshot[f"{kind}_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        manifest = {"schema_version": 1, "default_version": self.snapshot["version"], "snapshots": [self.snapshot]}
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return {"snapshot": self._metadata(), "directory": str(target.resolve())}
