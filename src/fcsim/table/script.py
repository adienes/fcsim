"""Drive a table from a written script, with no model: for tests and for rules scenarios."""

from __future__ import annotations

from .table import Table

_ZONE_ORDER = ("hand", "battlefield", "stack", "command", "graveyard", "exile", "library")


class ScriptError(AssertionError):
    """A scripted step did not go as the script said it would."""


class ScriptedRunner:
    """Each step is {seat, tool, args, expect?, error?}. A string argument `@Card Name` stands
    for that seat's handle for a card of that name, looked up the way an agent would: in its
    own view. `@Card Name#2` picks the second such card, and `$Card Name` is the stack item
    whose source has that name."""

    def __init__(self, table: Table):
        self.table = table
        self.results: list[dict] = []

    def handle(self, seat: int, reference: str) -> str:
        name, _, index = reference[1:].partition("#")
        view = self.table.view(seat)
        if reference.startswith("$"):  # a spell or ability on the stack, by the name of its source
            found = [item["handle"] for item in view["stack"] if item["source"] == name]
        else:
            found = [card["handle"] for card in self._cards(view) if card.get("name") == name]
        if len(found) < int(index or 1):
            raise ScriptError(f"Seat {seat} cannot see a card named {name!r} (looked for number {index or 1}).")
        return found[int(index or 1) - 1]

    @staticmethod
    def _cards(view: dict):
        you = view["you"]
        seats = sorted(view["seats"], key=lambda seat: seat["seat"] != you)  # your own zones first
        for zone in _ZONE_ORDER:
            if zone == "battlefield":
                yield from sorted(view["battlefield"], key=lambda card: card.get("controller") != you)
            elif zone == "stack":
                yield from ({"name": item["source"], "handle": item["card"]} for item in view["stack"] if "card" in item)
            else:
                for seat in seats:
                    found = seat[zone]
                    yield from found if isinstance(found, list) else found.get("cards", []) + found.get("known", [])

    def _fill(self, seat: int, value):
        if isinstance(value, dict):
            return {key: self._fill(seat, item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._fill(seat, item) for item in value]
        return self.handle(seat, value) if isinstance(value, str) and value[:1] in ("@", "$") else value

    def step(self, seat: int, tool: str, args: dict | None = None, *, error: str | None = None) -> dict:
        result = self.table.act(seat, tool, self._fill(seat, args or {}))
        self.results.append(result)
        code = result.get("error", {}).get("code")
        if code != error:
            raise ScriptError(f"Seat {seat} `{tool}`: expected {error or 'success'}, got {result}")
        return result

    def run(self, steps: list[dict]) -> list[dict]:
        return [self.step(s["seat"], s["tool"], s.get("args"), error=s.get("error")) for s in steps]
