"""A game's directory: everything needed to read, replay or resume it, written as the game goes."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from ..cards import CardDatabase
from ..cards.model import Card
from ..rules import RulesError, RulesReference


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def _passer(event: dict) -> str:
    """Who passed, and until when if they said: "Wilhelt (until end)"."""
    text = " ".join(event["public"])
    until = re.search(r"keep passing until the (\w+) step", text)
    return text.split(" passes priority")[0] + (f" (until {until[1]})" if until else "")


def table_facts(card: Card) -> dict:
    """What the card-blind table is handed about a card: numbers and words it carries but never interprets."""
    return card.table_facts()


class GameDirectory:
    """The event journal is appended at every commit, so an interrupted game loses nothing the
    table accepted. Snapshots and reports are written to a temporary file and moved into place."""

    def __init__(self, root: str | Path, label: str, seed: int):
        self.path = Path(root) / f"{datetime.now():%Y-%m-%d_%H%M%S}_{slug(label)}_{seed % 10000:04d}"
        (self.path / "decks").mkdir(parents=True)

    @classmethod
    def open(cls, path: str | Path) -> GameDirectory:
        """An existing game, to read it or to play on from where it stopped."""
        directory = cls.__new__(cls)
        directory.path = Path(path)
        if not (directory.path / "events.jsonl").is_file() or not (directory.path / "config.json").is_file():
            raise ValueError(f"{path} is not a game directory: it has no events.jsonl and config.json.")
        return directory

    def read(self, name: str):
        return json.loads((self.path / name).read_text(encoding="utf-8"))

    def events(self) -> list[dict]:
        return [json.loads(line) for line in (self.path / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]

    def write(self, name: str, content: str | dict | list) -> None:
        text = content if isinstance(content, str) else json.dumps(content, indent=1, ensure_ascii=False)
        partial = self.path / f".{name.replace('/', '_')}.partial"
        partial.write_text(text, encoding="utf-8")
        os.replace(partial, self.path / name)

    def append(self, name: str, record: dict) -> None:
        with (self.path / name).open("a", encoding="utf-8") as out:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")

    def journal(self, event: dict) -> None:
        """One committed event: the authoritative record, and the readable log beside it. The record
        keeps every pass of priority, because a replay needs them; the log, which is for reading, tells
        a run of passes in one line. Three quarters of the third pod's log was "passes priority"."""
        self.append("events.jsonl", event)
        passes = getattr(self, "_passes", [])
        if event["tool"] == "pass_priority" and not event["lints"] and (not passes or passes[-1]["turn"] == event["turn"]):
            self._passes = [*passes, event]
            return
        self.flush_log()
        if event["tool"] == "pass_priority" and not event["lints"]:
            self._passes = [event]
        elif event["public"] or event["lints"]:
            with (self.path / "log.md").open("a", encoding="utf-8") as out:
                out.write(f"- `{event['n']}` turn {event['turn']} {event['step']}: " + " ".join(event["public"]) + "\n")
                out.writelines(f"  - table warning: {lint}\n" for lint in event["lints"])

    def rewrite_log(self) -> None:
        """Write log.md again from the journal, for a game logged before passes were told in runs."""
        events = self.events()
        (self.path / "log.md").write_text("", encoding="utf-8")
        self._passes = []
        for event in events:
            passes = self._passes
            if event["tool"] == "pass_priority" and not event["lints"] and (not passes or passes[-1]["turn"] == event["turn"]):
                self._passes = [*passes, event]
                continue
            self.flush_log()
            if event["tool"] == "pass_priority" and not event["lints"]:
                self._passes = [event]
            elif event["public"] or event["lints"]:
                with (self.path / "log.md").open("a", encoding="utf-8") as out:
                    out.write(f"- `{event['n']}` turn {event['turn']} {event['step']}: " + " ".join(event["public"]) + "\n")
                    out.writelines(f"  - table warning: {lint}\n" for lint in event["lints"])
        self.flush_log()

    def flush_log(self) -> None:
        """Write the run of passes held back so far. Call it when a game stops."""
        passes, self._passes = getattr(self, "_passes", []), []
        if not passes:
            return
        who = list(dict.fromkeys(_passer(e) for e in passes))
        steps = list(dict.fromkeys(e["step"] for e in passes))
        numbers = f"{passes[0]['n']}" + (f"-{passes[-1]['n']}" if len(passes) > 1 else "")
        with (self.path / "log.md").open("a", encoding="utf-8") as out:
            out.write(f"- `{numbers}` turn {passes[0]['turn']} {steps[0]}" + (f" to {steps[-1]}" if len(steps) > 1 else "")
                      + f": {', '.join(who)} passed priority" + (f" ({len(passes)} passes)" if len(passes) > len(who) else "") + ".\n")

    def pin_cards(self, names: set[str], database: CardDatabase) -> None:
        """Oracle text and rulings as they were on the day, so the game stays readable after errata."""
        cards = {}
        for name in sorted(names):
            card = database.find(name)
            if card is not None:
                cards[name] = {**card.to_dict(), "rulings": database.rulings(card)}
        self.write("cards.json", cards)

    def card_lookup(self):
        """What the table was told about each card when this game was played, from the pinned copy.
        A replay must hand the table the same: triggers are queued by card type, so without it the
        rebuilt game takes another path and the journal stops fitting."""
        if not (self.path / "cards.json").is_file():
            return None
        cards = {name: Card.from_dict({k: v for k, v in data.items() if k != "rulings"}) for name, data in self.read("cards.json").items()}
        return lambda name: table_facts(cards[name]) if name in cards else None

    def pin_rules(self, rules: RulesReference | None) -> None:
        if rules is not None:
            try:
                rules.export_snapshot(self.path / "rules")
            except (RulesError, OSError):
                pass  # the game does not depend on it
