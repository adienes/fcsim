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

    def __init__(self, budget: Budget, *, cast_commander: bool = False):
        self.budget, self.prompts, self.cast_commander = budget, [], cast_commander

    def ask(self, system: str, prompt: str, *, tools: McpConnection | None = None, schema: dict | None = None, **_) -> Reply:
        self.budget.begin()
        self.prompts.append(prompt)
        if tools is None:  # a triage question: this scripted player never responds
            reply = Reply(data={"respond": False, "why": "scripted"}, text="", cost_usd=0.01, seconds=0.5, model=self.model)
            self.budget.record(reply)
            return reply
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
            elif self.cast_commander and waiting_for == "priority" and view["turn"]["active"] == me and not view["stack"] \
                    and view["turn"]["step"] == "main1" and view["seats"][me]["command"] \
                    and len([c for c in view["battlefield"] if c["controller"] == me and not c["tapped"]]) >= 4:
                lands = [c["handle"] for c in view["battlefield"] if c["controller"] == me and not c["tapped"]][:4]
                result = tool("cast", card=view["seats"][me]["command"][0]["handle"], payment={"tap": lands}, resolves="auto_permanent")
            elif waiting_for == "priority" and view["turn"]["active"] == me and view["stack"]:
                result = tool("pass_priority")   # my own spell is on the stack: let it resolve
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
    assert sum(p.count("Pir plays Forest") for p in later) == 2   # each of Pir's land plays reaches Talrand exactly once


def test_a_pod_game_stops_at_its_budget(database, tmp_path):
    budget = Budget(max_calls=4)
    report = play_pod(decks(tmp_path), [ScriptedSeat(budget) for _ in range(3)], FakeRunner([OK] * 9, budget), database,
                      cycles=2, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    assert "limit of 4 model calls" in report["stopped"] and (Path(report["directory"]) / "report.md").exists()


def test_a_pod_game_can_be_resumed_from_its_directory(database, tmp_path):
    from fcsim.play.gamedir import GameDirectory
    from fcsim.table import Table
    budget = Budget(max_calls=300)
    first = play_pod(decks(tmp_path), [ScriptedSeat(budget) for _ in range(3)], FakeRunner([OK] * 60, budget), database,
                     cycles=1, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    assert first["turns_played"] == 4 and [s["permanents"] for s in first["standings"]] == [1, 1, 1]
    seats = [ScriptedSeat(budget) for _ in range(3)]
    more = play_pod([], seats, FakeRunner([OK] * 60, budget), database, cycles=2, resume=first["directory"], echo=lambda *_: None)
    assert more["directory"] == first["directory"] and more["turns_played"] == 10 and more["stopped"] is None
    assert [s["permanents"] for s in more["standings"]] == [3, 3, 3]            # two more land drops each, on the same board
    assert len(more["wakes"]) > len(first["wakes"]) and more["wakes"][:len(first["wakes"])] == first["wakes"]
    assert more["earlier_usage"] == [first["usage"]]
    assert not any("Since your last wake" in p and "keeps 7 cards" in p for seat in seats for p in seat.prompts)  # no replayed history
    directory = GameDirectory.open(first["directory"])
    events = directory.events()
    assert [e["n"] for e in events] == list(range(len(events)))                  # one unbroken journal
    from fcsim.play.pod import _lookup
    replayed = Table.replay(directory.read("config.json")["table"], events, card_lookup=_lookup(database)).game.to_dict()
    assert json.loads(json.dumps(replayed)) == directory.read("state.json")    # the saved state is the journal's state
    with pytest.raises(ValueError, match="not a game directory"):
        play_pod([], seats, FakeRunner([OK], budget), database, resume=tmp_path)


def test_a_wake_starts_with_a_briefing_and_responses_are_triaged_in_parallel(database, tmp_path):
    from fcsim.play import pod

    budget = Budget(max_calls=400)
    seats = [ScriptedSeat(budget, cast_commander=True), ScriptedSeat(budget), ScriptedSeat(budget)]
    report = play_pod(decks(tmp_path), seats, FakeRunner([OK] * 120, budget), database, cycles=5, seed=4,
                      out_root=tmp_path / "games", echo=lambda *_: None)
    assert report["stopped"] is None
    briefed = seats[1].prompts[1]
    assert "THE TABLE (the codes in brackets are your handles" in briefed and "YOUR HAND\n- [c" in briefed
    assert "[Basic Land — Forest]" in briefed and "YOUR NOTES\n(none yet)" in briefed and "CARDS IN PLAY" in briefed
    assert "ruling " not in briefed                                        # card text, not rulings: a briefing stays short
    assert briefed.index("WHAT YOU HAVE ALREADY DONE THIS TURN") < briefed.index("THE TABLE (")
    # A player's whole turn is one session: when it passes and nobody responds, the same wake carries on.
    own_turn_wakes = {}
    for w in report["wakes"]:
        if w["kind"] == "priority":
            own_turn_wakes[(w["seat"], w["turn"])] = own_turn_wakes.get((w["seat"], w["turn"]), 0) + 1
    assert own_turn_wakes and set(own_turn_wakes.values()) == {1}
    cast_turn = next(w for w in report["wakes"] if w["seat"] == 0 and w["kind"] == "priority" and w["tool_calls"] > 8)
    assert cast_turn["tool_errors"] == []                                  # land, commander, responses checked, and on to the end: one wake
    asked = [t for t in report["triage"] if t["about"] == "Talrand, Sky Summoner"]
    assert sorted(t["seat"] for t in asked) == [1, 2] and not any(t["respond"] for t in asked)   # both other seats, in one round
    events = [json.loads(line) for line in (Path(report["directory"]) / "events.jsonl").read_text().splitlines()]
    talrands_turns = {e["turn"] for e in events if e["seat"] == 0 and e["tool"] == "play_land"}
    woken_to_pass = [w for w in report["wakes"] if w["kind"] == "priority" and w["seat"] != 0 and w["turn"] in talrands_turns]
    assert talrands_turns and woken_to_pass == []                          # on Talrand's turns nobody else was woken just to pass
    assert report["standings"][0]["commander_casts"] == {"Talrand, Sky Summoner": 1} and pod._TRIAGE["required"] == ["respond", "why"]


def test_status_reads_a_game_directory_while_it_is_being_written(database, tmp_path):
    from fcsim.cli import main
    from fcsim.play.status import render, status
    budget = Budget(max_calls=300)
    wrong = {"kind": "illegal", "seat": 0, "summary": "Talrand paid no life", "basis": "test", "fix": "set life to 39", "confidence": "high"}
    report = play_pod(decks(tmp_path), [ScriptedSeat(budget, cast_commander=True), ScriptedSeat(budget), ScriptedSeat(budget)],
                      FakeRunner([{"verdict": "findings", "findings": [wrong], "hooks": []}] + [OK] * 80, budget), database,
                      cycles=5, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    seen = status(report["directory"])
    assert seen["seats"] == ["Talrand", "Pir", "Thrasios"] and seen["standings"][0]["life"] == 39 and seen["winner"] is None
    time = seen["time"]
    assert time["wakes"] == len(report["wakes"]) and time["audits"] == len(report["audits"]) and time["triage_questions"] == 2
    assert time["calls_by_tool"]["play_land"] == 15 and time["wakes_the_driver_finished"] == 0
    assert seen["findings"] == [{"turn": 1, "kind": "illegal", "seat": 0, "summary": "Talrand paid no life"}]
    assert sum(row["wakes"] for row in seen["by_turn"].values()) == time["wakes"]
    text = render(seen)
    assert "BY TABLE TURN" in text and "respond-or-pass questions (0 said respond)" in text and "Talrand" in text
    assert main(["status", report["directory"]]) == 0 and main(["status", str(tmp_path)]) == 2


def test_a_standing_policy_stops_the_respond_or_pass_questions(database, tmp_path):
    from fcsim.play import pod
    from fcsim.table import Table
    table = Table([{"name": n, "commanders": [], "library": ["Island"] * 40} for n in ("Ann", "Bob", "Cat")], seed=1, first=0)
    for seat in range(3):
        table.act(seat, "keep")
    assert table.act(1, "set_response_policy", {"policy": "sometimes"})["error"]["code"] == "bad_policy"
    assert table.act(1, "set_response_policy", {"policy": "pass_unless_targeted"})["ok"]
    assert table.act(2, "set_response_policy", {"policy": "pass_always"})["ok"]
    assert table.view(1)["seats"][1]["response_policy"] == "pass_unless_targeted"
    assert "response_policy" not in table.view(0)["seats"][1] and "policy" not in json.dumps(table.act(0, "get_log"))   # private
    for _ in range(3):
        table.act(table.expected()[0][0], "pass_priority")
    while table.game.turn.step != "main1":
        table.act(table.expected()[0][0], "pass_priority")
    hand = table.view(0)["seats"][0]["hand"]["cards"]
    table.act(0, "cast", {"card": hand[0]["handle"], "targets": [{"seat": 2}]})
    assert [pod._wants_asking(table, seat) for seat in (1, 2)] == [False, False]      # not aimed at Bob; Cat never wants asking
    table.act(0, "cast", {"card": hand[1]["handle"], "targets": [{"seat": 1}]})
    assert [pod._wants_asking(table, seat) for seat in (1, 2)] == [True, False]       # now Bob is the target
    while table.game.turn.active == 0:
        seat, kind = table.expected()[0]
        table.act(seat, {"priority": "pass_priority", "resolution": "finish_resolution", "declare_attackers": "declare_attackers",
                         "discard": "finish_step"}[kind])
    assert table.game.seats[1].response_policy == "ask" and table.game.seats[2].response_policy == "pass_always"   # reset on one's own turn only
