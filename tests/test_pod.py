from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from fcsim.agents import Budget, FakeRunner, Reply
from fcsim.cards import CardDatabase
from fcsim.mcp import McpConnection
from fcsim.play import play_pod

OK = {"verdict": "ok", "findings": [], "hooks": []}


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    path = tmp_path_factory.mktemp("cards") / "cards.sqlite"
    fixture = Path(__file__).parent / "data" / "cards.jsonl"
    CardDatabase.build([json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()], path,
                       source="fixture", updated_at="")
    return CardDatabase(path)


def decks(tmp_path: Path) -> list[Path]:
    lists = {"talrand": ("Island", "Talrand, Sky Summoner"), "pir": ("Forest", "Pir, Imaginative Rascal"),
             "thrasios": ("Island", "Thrasios, Triton Hero")}
    paths = []
    for name, (land, commander) in lists.items():
        paths.append(tmp_path / f"{name}.txt")
        paths[-1].write_text(f"99 {land}\n\n1 {commander}\n", encoding="utf-8")
    return paths


class ScriptedSeat:
    """Stands in for a model at any seat, playing through the real MCP server: a land on its own
    main phase, no attacks, no responses; it answers whatever else the table asks of it."""

    model = "scripted"

    def __init__(self, budget: Budget):
        self.budget, self.prompts = budget, []

    def ask(self, system: str, prompt: str, *, tools: McpConnection, **_) -> Reply:
        self.budget.begin()
        self.prompts.append(prompt)
        count = [0]

        def tool(tool_name: str, **arguments) -> dict:
            count[0] += 1
            request = urllib.request.Request(tools.url, data=json.dumps(
                {"jsonrpc": "2.0", "id": count[0], "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}}).encode(),
                headers={**tools.headers, "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(json.loads(response.read())["result"]["content"][0]["text"])

        for _ in range(40):
            view = tool("get_view")
            if "error" in view:
                break
            me, waiting_for = view["you"], view["pen"].get("action") or view["pen"]["kind"]
            if me not in view["pen"]["seats"]:
                break
            land = next((c["handle"] for c in view["seats"][me]["hand"]["cards"] if c["name"] in ("Island", "Forest")), None)
            if waiting_for == "setup":
                result = tool("keep")
            elif waiting_for == "triggers":
                result = tool("add_trigger", pending=next(t["handle"] for t in view["waiting_triggers"] if t["seat"] == me))
            elif waiting_for == "correction":
                tool("act", because="correcting my life total", mutations=[{"op": "life", "seat": me, "set": 39}])
                result = tool("finish_step")
            elif waiting_for == "priority" and view["turn"]["active"] == me and view["turn"]["step"] == "main1" \
                    and land and not view["turn"]["land_plays"]:
                result = tool("play_land", card=land)
            elif waiting_for == "priority" and view["turn"]["active"] == me and not view["stack"]:
                step = view["turn"]["step"]
                result = tool("pass_priority", until="main1") if step in ("upkeep", "draw") else \
                    tool("pass_priority") if step == "end" else tool("pass_priority", until="end")
            else:
                result = tool({"priority": "pass_priority", "discard": "finish_step", "held_step": "finish_step",
                               "resolution": "finish_resolution"}.get(waiting_for, waiting_for))
            if "wake" in result or "error" in result:
                break
        reply = Reply(data=None, text="Played a land if I could.", cost_usd=0.02, seconds=1.0, model=self.model)
        self.budget.record(reply)
        return reply


def test_a_pod_game_schedules_seats_audits_and_acts_on_findings(database, tmp_path):
    budget = Budget(max_calls=200)
    seats = [ScriptedSeat(budget) for _ in range(3)]
    trigger = {"kind": "trigger", "seat": 2, "summary": "Thrasios's imaginary trigger", "basis": "test", "fix": "put it on the stack",
               "confidence": "high"}
    wrong = {"kind": "illegal", "seat": 0, "summary": "Talrand paid no life", "basis": "test", "fix": "set life to 39", "confidence": "high"}
    verdicts = [{"verdict": "findings", "findings": [trigger], "hooks": []}, {"verdict": "findings", "findings": [wrong], "hooks": []}] + [OK] * 60
    report = play_pod(decks(tmp_path), seats, FakeRunner(verdicts, budget), database, cycles=2, seed=4,
                      out_root=tmp_path / "games", echo=lambda *_: None)
    assert report["stopped"] is None and report["seats"] == ["Talrand", "Pir", "Thrasios"] and report["turns_played"] == 7
    kinds = {(w["seat"], w["kind"]) for w in report["wakes"]}
    assert {(0, "mulligan"), (1, "mulligan"), (2, "mulligan"), (2, "triggers"), (0, "correction")} <= kinds
    assert not any(w["forced"] for w in report["wakes"]) and report["standings"][0]["life"] == 39
    assert [s["permanents"] for s in report["standings"]] == [2, 2, 2]          # a land on each of two turns, for every seat
    # A seat is not woken to pass on someone else's turn when nothing is on the stack ...
    priority_wakes = [w for w in report["wakes"] if w["kind"] == "priority"]
    assert all(w["seat"] == (w["turn"] - 1) % 3 or w["events"][1] > w["events"][0] for w in priority_wakes)
    # ... but is woken to respond once something is: the queued trigger went on the stack.
    directory = Path(report["directory"])
    log = (directory / "log.md").read_text()
    assert "A trigger is waiting for Thrasios" in log and "The auditor asks Talrand to correct the table" in log
    assert "Talrand has finished correcting the table." in log and len(report["audits"]) >= 6
    assert {"config.json", "cards.json", "events.jsonl", "audits.jsonl", "report.md", "state.json", "decks",
            "transcript-seat-0.jsonl", "transcript-seat-2.jsonl"} <= {p.name for p in directory.iterdir()}
    assert "| Talrand | alive | 39 |" in (directory / "report.md").read_text()
    later = [p for p in seats[0].prompts if "Since your last wake:" in p and "Pir plays Forest" in p]
    assert len(later) == 2   # each of Pir's two land plays reaches Talrand once, in whichever wake comes next


def test_a_pod_game_stops_at_its_budget(database, tmp_path):
    budget = Budget(max_calls=4)
    report = play_pod(decks(tmp_path), [ScriptedSeat(budget) for _ in range(3)], FakeRunner([OK] * 9, budget), database,
                      cycles=2, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    assert "limit of 4 model calls" in report["stopped"] and (Path(report["directory"]) / "report.md").exists()
