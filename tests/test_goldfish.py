from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from fcsim.agents import Budget, FakeRunner, Reply
from fcsim.cards import CardDatabase
from fcsim.play import goldfish
from fcsim.play.goldfish import GOLDFISH

DECK = "1 Sol Ring\n1 Counterspell\n1 Opt\n96 Island\n\n1 Talrand, Sky Summoner\n"


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    path = tmp_path_factory.mktemp("cards") / "cards.sqlite"
    fixture = Path(__file__).parent / "data" / "cards.jsonl"
    CardDatabase.build([json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()], path,
                       source="fixture", updated_at="")
    return CardDatabase(path)


class ScriptedPilot:
    """Stands in for the model: it plays through the real MCP server, as a headless agent would.
    Each turn it plays an Island if it holds one and then passes until its wake is over."""

    model = "scripted"

    def __init__(self, budget: Budget):
        self.budget, self.prompts, self.systems = budget, [], []

    def ask(self, system: str, prompt: str, *, mcp_config: dict, allowed_tools: tuple[str, ...], **_) -> Reply:
        self.budget.check()
        self.prompts.append(prompt), self.systems.append(system)
        server = mcp_config["mcpServers"]["fcsim"]
        count = [0]

        def rpc(method: str, params: dict | None = None) -> dict:
            count[0] += 1
            request = urllib.request.Request(server["url"], data=json.dumps(
                {"jsonrpc": "2.0", "id": count[0], "method": method, "params": params or {}}).encode(),
                headers={**server["headers"], "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read())["result"]

        def tool(tool_name: str, **arguments) -> dict:
            return json.loads(rpc("tools/call", {"name": tool_name, "arguments": arguments})["content"][0]["text"])

        assert rpc("initialize", {"protocolVersion": "2025-06-18"})["capabilities"] == {"tools": {}}
        offered = {t["name"] for t in rpc("tools/list")["tools"]}
        assert {f"mcp__fcsim__{name}" for name in offered} == set(allowed_tools) and "lookup_card" in offered
        view = tool("get_view")
        if view["pen"]["kind"] == "setup":
            tool("keep")
        else:
            assert tool("lookup_card", name="opt")["name"] == "Opt"
            island = next((c["handle"] for c in view["seats"][0]["hand"]["cards"] if c["name"] == "Island"), None)
            for _ in range(40):
                waiting_for = view["pen"].get("action") or view["pen"]["kind"]
                result = tool("play_land", card=island) if island and view["turn"]["step"] == "main1" else \
                    tool({"priority": "pass_priority", "discard": "finish_step", "held_step": "finish_step"}.get(waiting_for, waiting_for))
                island = None if "ok" in result and result["log"][0].startswith("Pilot plays") else island
                if "wake" in result or "error" in result:
                    assert tool("pass_priority")["error"]["code"] == "stale_wake" if "wake" in result else True
                    break
                view = tool("get_view")
        reply = Reply(data=None, text="Played a land and passed.", cost_usd=0.02, seconds=1.0, model=self.model)
        self.budget.record(reply)
        return reply


def test_a_goldfish_run_plays_audits_and_writes_a_game_directory(database, tmp_path):
    deck = tmp_path / "talrand.txt"
    deck.write_text(DECK, encoding="utf-8")
    budget = Budget(max_calls=50)
    hook = {"kind": "hold", "source": "Island", "step": "untap", "text": "a test hook"}
    verdicts = [{"verdict": "ok", "findings": [], "hooks": [hook] if turn == 0 else []} for turn in range(3)]
    pilot = ScriptedPilot(budget)
    report = goldfish(deck, pilot, FakeRunner(verdicts, budget), database, turns=3, seed=11, out_root=tmp_path / "games",
                      echo=lambda *_: None)
    assert report["stopped"] is None and [t["turn"] for t in report["turns"]] == [0, 1, 2, 3]
    assert "YOUR DECK" in pilot.systems[0] and "Talrand, Sky Summoner {2}{U}{U}" in pilot.systems[0]
    assert pilot.prompts[1].startswith("It is your turn 1 (table turn 1)") and "turn 3 (table turn 5)" in pilot.prompts[3]
    assert report["metrics"]["turns"][1]["lands"] == 1 and report["metrics"]["goldfish_life"] == 40
    assert report["turns"][1]["audit"]["verdict"] == "ok" and report["turns"][1]["hooks_applied"]
    held = [c for c in json.loads((Path(report["directory"]) / "transcript-seat-0.jsonl").read_text().splitlines()[2])["calls"]
            if c["tool"] == "finish_step"]
    assert held   # the hook from turn 1's audit was applied in time to hold the pilot's very next untap step
    assert not any(t["forced_to_end"] for t in report["turns"])
    assert {e["error"] for t in report["turns"] for e in t["tool_errors"]} == {"stale_wake"}  # only the pilot's probe after its wake ended
    assert budget.calls == 7   # four pilot wakes and three audits

    directory = Path(report["directory"])
    assert "_goldfish_talrand-sky-summoner_" in directory.name
    files = {p.name for p in directory.iterdir()}
    assert {"config.json", "cards.json", "events.jsonl", "log.md", "state.json", "audits.jsonl", "report.md",
            "report.json", "usage.json", "transcript-seat-0.jsonl", "decks"} <= files
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    assert [e["n"] for e in events] == list(range(len(events))) and "Pilot plays Island" in (directory / "log.md").read_text()
    goldfish_turns = {e["turn"] for e in events if e["seat"] == GOLDFISH and e["turn"] > 0}
    assert goldfish_turns and all(e["tool"] in ("keep", "pass_priority", "declare_attackers", "finish_step")
                                  for e in events if e["seat"] == GOLDFISH)
    assert "| 1 | 1 | - | - | ok |" in (directory / "report.md").read_text()


def test_a_goldfish_run_stops_cleanly_at_its_budget(database, tmp_path):
    deck = tmp_path / "talrand.txt"
    deck.write_text(DECK.replace("96 Island", "100 Island"), encoding="utf-8")   # 104 cards: still playable
    budget = Budget(max_calls=2)
    report = goldfish(deck, ScriptedPilot(budget), FakeRunner([{"verdict": "ok", "findings": [], "hooks": []}] * 5, budget),
                      database, turns=5, seed=11, out_root=tmp_path / "games", echo=lambda *_: None)
    assert "limit of 2 model calls" in report["stopped"]
    assert "Played as listed although: The deck has 104 cards" in (Path(report["directory"]) / "report.md").read_text()
