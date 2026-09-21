from __future__ import annotations

import json
import re
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

    def __init__(self, budget: Budget, *, cast_commander: bool = False, with_trigger: bool = False, triage: dict | None = None,
                 attack: int | None = None, contests: bool = False, disputes: str = "", mulligans: int = 0, bottoms: bool = True,
                 acts: bool = False, holds: bool = False, fidgets: bool = False, asks: str = "", answers: str = ""):
        self.budget, self.prompts, self.cast_commander, self.attack, self.contests = budget, [], cast_commander, attack, contests
        self.disputes, self.mulligans, self.bottoms, self.acts, self.holds = disputes, mulligans, bottoms, acts, holds
        self.asks, self.answers, self.heard = asks, answers, []   # a question for seat 1, once; what this seat says when spoken to
        self.fidgets = fidgets  # does something auditable each turn: a basic land alone is not sent to the auditor
        self.with_trigger, self.triage = with_trigger, triage or {}   # a made-up trigger on top of the commander; triage answers

    def ask(self, system: str, prompt: str, *, tools: McpConnection | None = None, schema: dict | None = None, **_) -> Reply:
        self.budget.begin()
        self.prompts.append(prompt)
        if tools is None:  # one question and no tools: this scripted player keeps every hand and never responds
            if "keep" in (schema or {}).get("properties", {}):
                owed = int(re.search(r"you owe (\d+)", prompt)[1]) if self.bottoms else 0
                data = {"keep": self.mulligans <= 0, "bottom": re.findall(r"^- \[(c\d+)\]", prompt.split("YOUR HAND")[1], re.M)[:owed], "why": "scripted"}
                self.mulligans -= 1
            elif "reply" in (schema or {}).get("properties", {}):
                data = {"reply": self.answers}
            elif "act" in (schema or {}).get("properties", {}):
                data = {"act": self.acts, "why": "scripted"}
            else:
                data = {"respond": False, "why": "scripted", **self.triage}
            reply = Reply(data=data, text="", cost_usd=0.01, seconds=0.5, model=self.model)
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
            if self.disputes and waiting_for == "priority":
                tool("dispute", claim=self.disputes)
                self.disputes = ""
            if self.holds and waiting_for == "priority" and view["turn"]["active"] == me and not view["seats"][me]["holds_for_end_step"]:
                tool("set_response_policy", policy="ask", hold_for_end_step=True)
            if self.asks and waiting_for == "priority" and view["turn"]["active"] == me:
                self.heard.append(tool("say", text=self.asks, to=[1, 2]).get("replies"))
                self.asks = ""
            if waiting_for == "setup":
                result = tool("keep")
            elif waiting_for == "triggers":
                result = tool("add_trigger", pending=next(t["handle"] for t in view["waiting_triggers"] if t["seat"] == me))
            elif waiting_for == "correction" and self.contests and not view["pen"].get("final"):
                result = tool("contest", reason="I paid 1 life, as rule 119.4 says a payment of life is made. Rule 999.9z agrees.")
            elif waiting_for == "correction":
                tool("act", because="correcting my life total", mutations=[{"op": "life", "seat": me, "set": 39}])
                result = tool("finish_step")
            elif waiting_for == "priority" and view["turn"]["active"] == me and view["turn"]["step"] == "main1" \
                    and land and not view["turn"]["land_plays"]:
                result = tool("play_land", card=land)
                if self.fidgets:
                    result = tool("act", because="Tapping and untapping my land, to no purpose.", mutations=[{"op": "life", "seat": me, "delta": 0}])
            elif self.cast_commander and waiting_for == "priority" and view["turn"]["active"] == me and not view["stack"] \
                    and view["turn"]["step"] == "main1" and view["seats"][me]["command"] \
                    and len([c for c in view["battlefield"] if c["controller"] == me and not c["tapped"]]) >= 4:
                lands = [c["handle"] for c in view["battlefield"] if c["controller"] == me and not c["tapped"]][:4]
                result = tool("cast", card=view["seats"][me]["command"][0]["handle"], payment={"tap": lands}, resolves="auto_permanent")
            elif self.with_trigger and waiting_for == "priority" and [i["kind"] for i in view["stack"]] == ["spell"]:
                self.with_trigger = False
                result = tool("add_trigger", text="Whenever you cast your commander, scry 1.")
            elif waiting_for == "priority" and view["turn"]["active"] == me and view["stack"]:
                result = tool("pass_priority")   # my own spell is on the stack: let it resolve
            elif waiting_for == "declare_attackers" and self.attack is not None:
                result = tool("declare_attackers", attacks=[{"attacker": c["handle"], "target": {"seat": self.attack}} for c in view["battlefield"]
                                                            if c["controller"] == me and c.get("is_commander") and not c.get("entered_this_turn")])
            elif waiting_for == "combat_damage" and self.attack is not None:
                result = tool("combat_damage", assignments=[{"source": a["attacker"], "target": {"seat": self.attack}, "amount": 2}
                                                            for a in view["turn"]["attackers"]])
            elif self.attack is not None and waiting_for == "priority" and view["turn"]["active"] == me and not view["stack"] \
                    and view["turn"]["step"] in ("main1", "begin_combat"):
                result = tool("pass_priority", until="begin_combat") if view["turn"]["step"] == "main1" else tool("pass_priority")
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
    seats = [ScriptedSeat(budget, fidgets=True) for _ in range(3)]
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
    # Opening hands are decided by all seats at once, with no tools; nobody is handed priority in an upkeep.
    assert [w["tool_calls"] for w in report["wakes"] if w["kind"] == "mulligan"] == [0, 0, 0]
    assert "This is your opening hand, after 0 mulligan(s)." in seats[0].prompts[0] and "YOUR HAND\n- [c" in seats[0].prompts[0]
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
    assert report["standings"][0]["commander_casts"] == {"Talrand, Sky Summoner": 1} and pod._TRIAGE["required"] == ["respond", "ask_again", "why"]


def test_status_reads_a_game_directory_while_it_is_being_written(database, tmp_path):
    from fcsim.cli import main
    from fcsim.play.status import render, status
    budget = Budget(max_calls=300)
    wrong = {"kind": "illegal", "seat": 0, "summary": "Talrand paid no life", "basis": "test", "fix": "set life to 39", "confidence": "high"}
    report = play_pod(decks(tmp_path), [ScriptedSeat(budget, cast_commander=True, fidgets=True), ScriptedSeat(budget, fidgets=True), ScriptedSeat(budget)],
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


def test_summarize_condenses_each_round_once_and_writes_the_story(database, tmp_path):
    from fcsim.play.narrative import summarize

    class Writer:
        """Stands in for the model: answers with text and records what it was asked."""
        model, backend = "scripted", "fake"

        def __init__(self):
            self.asked, self.budget = [], Budget(max_calls=50)

        def ask(self, system, prompt, **_):
            self.budget.begin()
            self.asked.append((system, prompt))
            return Reply(data=None, text=f"An account ({len(self.asked)}).", cost_usd=0.01, seconds=0.1, model=self.model)

    budget = Budget(max_calls=300)
    game = play_pod(decks(tmp_path), [ScriptedSeat(budget) for _ in range(3)], FakeRunner([OK] * 60, budget), database,
                    cycles=2, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    writer = Writer()
    result = summarize(game["directory"], writer, database, style="recap", echo=lambda *_: None)
    directory = Path(game["directory"])
    written = (directory / "narrative.md").read_text()
    assert result["rounds"] == 2 and written.startswith("An account (3).\n\n## Appendix")           # two rounds, then the story
    assert "### Round 1\n\nAn account (1).\n\n### Round 2\n\nAn account (2).\n" in written and not (directory / "narrative").exists()
    first = writer.asked[0][1]
    assert first.startswith("ROUND 1 (table turns 1 to 3)") and "Pir plays Forest" in first and "[private to Pir: Drew" in first
    assert "passes priority" not in first and "THE PLAYERS' OWN ACCOUNTS" in first and "- Pir (turn 1, priority): Played a land" in first
    story_system, story_prompt = writer.asked[-1]
    assert "A clear recap" in story_system and "4 to 10 rounds" in story_system
    assert "THE POD" in story_prompt and "- Talrand: alive, 40 life; commander: Talrand, Sky Summoner" in story_prompt and "ROUND 2\nAn account (2)." in story_prompt
    assert "MEASURED FROM THE LOG" in story_prompt and "unfinished    length" in story_prompt       # the judgement starts from numbers
    again = Writer()
    summarize(game["directory"], again, database, style="analysis", echo=lambda *_: None)
    assert len(again.asked) == 2          # round 1 is reused; the last round of an unfinished game and the story are rewritten
    with pytest.raises(ValueError, match="style"):
        summarize(game["directory"], again, database, style="epic")


def test_the_driver_applies_a_loss_before_anyone_gets_priority(database, tmp_path):
    from fcsim.play import pod
    from fcsim.table import Table
    table = Table([{"name": n, "commanders": [], "library": ["Island"] * 40} for n in ("Ann", "Bob", "Cat")], seed=1, first=0)
    for seat in range(3):
        table.act(seat, "keep")
    assert pod._must_lose(table) == []
    table.patch([{"op": "life", "seat": 1, "set": -1}, {"op": "counters", "seat": 2, "kind": "poison", "set": 10}], "set up")
    assert pod._must_lose(table) == [(1, "-1 life, rule 704.5a"), (2, "10 poison counters, rule 704.5c")]
    table.game.ledger.append({"kind": "hold", "step": "loss", "seat": 1, "text": "Platinum Angel"})
    assert pod._must_lose(table) == [(2, "10 poison counters, rule 704.5c")]

    budget = Budget(max_calls=300)
    burn = {"kind": "note", "summary": "n/a", "basis": "", "fix": "", "confidence": "low"}
    seats = [ScriptedSeat(budget) for _ in range(3)]
    original = seats[1].ask

    def lose_life_then_play(system, prompt, **kw):   # Pir (the first player) drops Thrasios to 0 on its first turn
        if kw.get("tools") is not None and "Round 1, Pir" in prompt and not getattr(lose_life_then_play, "done", False):
            lose_life_then_play.done = True
            request = urllib.request.Request(kw["tools"].url, data=json.dumps({"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {
                "name": "act", "arguments": {"because": "test burn", "mutations": [{"op": "life", "seat": 2, "set": 0}]}}}).encode(),
                headers={**kw["tools"].headers, "Content-Type": "application/json"})
            urllib.request.urlopen(request, timeout=10).read()
        return original(system, prompt, **kw)
    seats[1].ask = lose_life_then_play
    report = play_pod(decks(tmp_path), seats, FakeRunner([{"verdict": "ok", "findings": [burn], "hooks": []}] * 40, budget), database,
                      cycles=2, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    log = (Path(report["directory"]) / "log.md").read_text()
    assert "state-based action: Thrasios loses (0 life, rule 704.5a)" in log and report["standings"][2]["status"] == "eliminated"
    after = log.split("Thrasios loses")[1]
    assert "Thrasios passes priority" not in after and "Thrasios plays" not in after      # it never acted again
    assert report["stopped"] is None and report["standings"][0]["permanents"] == 2


def test_notes_and_a_response_policy_can_still_be_saved_after_a_wake_has_ended():
    from fcsim.table import Table
    table = Table([{"name": n, "commanders": [], "library": ["Island"] * 40} for n in ("Ann", "Bob")], seed=1, first=0)
    wake = table.open_wake(0)
    table.call(wake, "1", "keep")
    table.close_wake(wake)
    assert table.call(wake, "2", "write_notes", {"text": "Bob is the threat."})["ok"] and table.notes[0] == "Bob is the threat."
    assert table.call(wake, "3", "set_response_policy", {"policy": "pass_always"})["ok"]
    assert "seats" in table.call(wake, "4", "get_view") and table.call(wake, "5", "pass_priority")["error"]["code"] == "stale_wake"


@pytest.mark.parametrize("answer, questions", [({}, 4), ({"ask_again": True}, 4), ({"ask_again": False}, 2)])
def test_one_answer_can_cover_everything_on_the_stack(database, tmp_path, answer, questions):
    budget = Budget(max_calls=400)
    seats = [ScriptedSeat(budget, cast_commander=True, with_trigger=True), ScriptedSeat(budget, triage=answer), ScriptedSeat(budget, triage=answer)]
    report = play_pod(decks(tmp_path), seats, FakeRunner([OK] * 120, budget), database, cycles=5, seed=4,
                      out_root=tmp_path / "games", echo=lambda *_: None)
    assert report["stopped"] is None and report["standings"][0]["commander_casts"] == {"Talrand, Sky Summoner": 1}
    # Talrand's commander and a trigger above it: two other seats, asked once each, or once per item.
    assert [(t["about"], t["stack"]) for t in report["triage"]].count(("a triggered ability", 2)) == 2
    assert len(report["triage"]) == questions
    asked = next(p for p in seats[1].prompts if "The stack, top first:" in p)
    assert "1. Talrand's a triggered ability: Whenever you cast your commander, scry 1.  (new since you were last asked)" in asked
    assert "2. Talrand's Talrand, Sky Summoner  (new since you were last asked)" in asked


def test_a_triage_answer_can_set_a_standing_policy(database, tmp_path):
    budget = Budget(max_calls=400)
    seats = [ScriptedSeat(budget, cast_commander=True, with_trigger=True), ScriptedSeat(budget, triage={"policy": "pass_always"}), ScriptedSeat(budget)]
    report = play_pod(decks(tmp_path), seats, FakeRunner([OK] * 120, budget), database, cycles=5, seed=4,
                      out_root=tmp_path / "games", echo=lambda *_: None)
    assert report["stopped"] is None
    assert [t["seat"] for t in report["triage"]].count(1) == 1 and [t["seat"] for t in report["triage"]].count(2) == 2


@pytest.fixture(scope="module")
def combat_game(database, tmp_path_factory):
    """Talrand and Pir cast their commanders; Talrand then attacks Pir on each of its turns."""
    import re

    class Auditing(FakeRunner):
        def ask(self, system, prompt, *, schema=None, **_):
            if schema and schema["required"] == ["computed"]:
                handle = re.search(r"Talrand, Sky Summoner \[(c\d+)\]", prompt)[1]
                self.answers.insert(0, {"computed": [{"handle": handle, "power": "4", "toughness": "4", "because": "a test anthem"}]})
            return super().ask(system, prompt, schema=schema)

    tmp_path = tmp_path_factory.mktemp("combat")
    budget = Budget(max_calls=500)
    seats = [ScriptedSeat(budget, cast_commander=True, attack=1), ScriptedSeat(budget, cast_commander=True), ScriptedSeat(budget)]
    auditor = Auditing([OK] * 150, budget)
    report = play_pod(decks(tmp_path), seats, auditor, database, cycles=6, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    return report, seats, auditor


def test_power_and_toughness_are_worked_out_again_before_blocks(combat_game):
    report, seats, auditor = combat_game
    assert report["stopped"] is None and report["standings"][1]["commander_damage"]
    # Talrand attacks Pir, who has a creature to block with, on two turns. The battlefield changed before the
    # first (nobody had worked anything out yet) and before the second (a land), so both are worked out afresh.
    assert [r["computed"][0]["power"] for r in report["refreshes"]] == ["4", "4"]
    asked = [a for a in auditor.asked if a["schema"]["required"] == ["computed"]]
    assert "THIS TURN SO FAR" in asked[0]["prompt"] and "Talrand attacks" not in asked[0]["system"]
    blocking = next(p for p in seats[1].prompts if "declare blockers" in p)
    assert "NOW 4/4 (a test anthem" in blocking                                # the blocker reads the fresh numbers


def test_a_game_is_scored_against_the_rubric_from_its_log(combat_game, capsys):
    from fcsim.cli import main
    from fcsim.play.rubric import measure

    report = combat_game[0]
    directory = Path(report["directory"])
    scored = measure(directory)
    assert scored == report["rubric"] == json.loads((directory / "rubric.json").read_text())
    talrand, pir, thrasios = scored["seats"]
    assert (talrand["attackers"], talrand["damage_to_players"], talrand["spells"], talrand["idle_turns"]) == (2, 4, 1, 1)
    assert pir["attacked_by_others"] == 2 and pir["blockers"] == 0 and thrasios["spells"] == 0
    assert (thrasios["turns"], thrasios["turns_from_third"], thrasios["idle_turns"]) == (6, 4, 4)   # lands and passes, all game
    verdicts = {s["measure"]: s["verdict"] for s in scored["scores"]}
    assert verdicts["length"] == "unfinished" and verdicts["participation"] == "bad" and verdicts["interaction"] == "bad"
    assert verdicts["threat rotation"] == "mixed" and "ending" not in verdicts                    # only Pir was ever leaned on
    assert "Thrasios" in next(s["finding"] for s in scored["scores"] if s["measure"] == "spotlight")
    assert "## Against the rubric" in (directory / "report.md").read_text()
    assert main(["rubric", str(directory)]) == 0
    assert "Thrasios did nothing on 4 of 4 turns" in capsys.readouterr().out


@pytest.mark.parametrize("decision, life, failed", [("overrule", 40, 0), ("uphold", 39, 1)])
def test_a_player_can_contest_a_finding_and_a_judge_rules(database, tmp_path, decision, life, failed):
    from fcsim.rules import RulesReference

    budget = Budget(max_calls=300)
    seats = [ScriptedSeat(budget, contests=True, fidgets=True), ScriptedSeat(budget, fidgets=True), ScriptedSeat(budget, fidgets=True)]
    wrong = {"kind": "illegal", "seat": 0, "summary": "Talrand paid no life", "basis": "rule 119.4", "fix": "set life to 39", "confidence": "high"}
    auditor = FakeRunner([{"verdict": "findings", "findings": [wrong], "hooks": []}] + [OK] * 60, budget)
    judge = FakeRunner([{"decision": decision, "explanation": "Nothing in the record cost life.", "fix": "",
                         "ruling": "A land drop costs no life."}], budget)
    report = play_pod(decks(tmp_path), seats, auditor, database, cycles=2, seed=4, out_root=tmp_path / "games", echo=lambda *_: None,
                      judge_runner=judge, rules=RulesReference(Path(__file__).resolve().parents[1] / "rules"))
    assert report["stopped"] is None and report["standings"][0]["life"] == life
    ruling = report["rulings"][0]
    assert (ruling["decision"], ruling["seat"], ruling["objection"][:17]) == (decision, 0, "I paid 1 life, as")
    asked = judge.asked[0]["prompt"]
    assert "THE AUDITOR'S FINDING (about Talrand)\nTalrand paid no life Basis: rule 119.4" in asked and "THE PLAYER'S OBJECTION\nI paid 1 life" in asked
    assert "RULES CITED\n119.4" in asked and "999.9z: there is no rule with this number." in asked     # a rule number cannot be bluffed
    assert "WHAT WAS DONE" in asked and "Pir plays Forest" in asked and judge.asked[0]["schema"]["required"][0] == "decision"
    directory = Path(report["directory"])
    log = (directory / "log.md").read_text()
    assert "Talrand contests the auditor's finding" in log and f"The judge {decision}" in log.replace("upholds", "uphold").replace("overrules", "overrule")
    # The ruling binds the rest of the game: every later audit and every later briefing carries it.
    assert "RULINGS ALREADY MADE IN THIS GAME\n- (turn 1) A land drop costs no life." in auditor.asked[-1]["prompt"]
    assert "THE JUDGE'S RULINGS IN THIS GAME" in seats[1].prompts[-1] and "- A land drop costs no life." in seats[1].prompts[-1]
    corrections = [w for w in report["wakes"] if w["kind"] == "correction"]
    assert len(corrections) == (1 if decision == "overrule" else 2)               # upheld: back to the player, with no appeal
    if decision == "uphold":
        assert "The ruling is final." in seats[0].prompts[[w["kind"] for w in report["wakes"] if w["seat"] == 0].index("correction") + 1]
    state = json.loads((directory / "state.json").read_text())
    assert state["seats"][0]["failed_disputes"] == failed and "1 contested (1 " + decision in (directory / "report.md").read_text()


def test_a_players_dispute_is_put_to_the_auditor_and_answered_in_public(database, tmp_path):
    import re

    class Answering(FakeRunner):
        def ask(self, system, prompt, *, schema=None, **_):
            raised = re.search(r"event (\d+) \(Thrasios, dispute", prompt)
            if raised:
                self.answers.insert(0, {**OK, "disputes": [{"event": int(raised[1]), "upheld": False, "answer": "A land drop has no cost."}]})
            return super().ask(system, prompt, schema=schema)

    budget = Budget(max_calls=300)
    seats = [ScriptedSeat(budget), ScriptedSeat(budget), ScriptedSeat(budget, disputes="Pir never paid for that Forest.")]
    auditor = Answering([OK] * 60, budget)
    report = play_pod(decks(tmp_path), seats, auditor, database, cycles=2, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    assert report["stopped"] is None
    log = (Path(report["directory"]) / "log.md").read_text()
    assert "Thrasios disputes what was done: Pir never paid for that Forest. The auditor will answer at its next check." in log
    assert log.count("The auditor rejects Thrasios's dispute: A land drop has no cost.") == 1
    state = json.loads((Path(report["directory"]) / "state.json").read_text())
    assert [seat["failed_disputes"] for seat in state["seats"]] == [0, 0, 1]
    assert "Disputes and rulings." in auditor.asked[0]["system"]


def test_a_deck_is_reviewed_over_its_games_and_only_sound_swaps_are_offered(combat_game, database, tmp_path, capsys):
    from fcsim.cli import main
    from fcsim.play.cardlog import card_record
    from fcsim.workshop.review import apply, review

    report = combat_game[0]
    record = card_record(report["directory"])
    talrand = record["seats"][0]["cards"]
    assert [r["name"] for r in talrand if r["seen_round"] == 0] == ["Island"] * 7 and record["rounds"] == report["rounds"]
    commander = next(r for r in talrand if r["commander"])
    assert (commander["played_round"], commander["times_cast"], commander["ended_in"], commander["seen_round"]) == (4, 1, "battlefield", None)
    assert sum(1 for r in talrand if r["played_as"] == "land or put into play") == 6 and max(r["rounds_in_hand"] for r in talrand) >= 4

    deck = tmp_path / "talrand.txt"
    deck.write_text("98 Island\n1 Opt\n\n1 Talrand, Sky Summoner\n", encoding="utf-8")
    proposed = [{"cut": "Island", "add": "counterspell", "reason": "No interaction.", "evidence": "Rounds 1 to 6: nothing but lands.", "expected_effect": "More interaction."},
                {"cut": "Island", "add": "Cultivate", "reason": "Ramp.", "evidence": "-", "expected_effect": "-"},
                {"cut": "Island", "add": "Black Lotus", "reason": "Speed.", "evidence": "-", "expected_effect": "-"},
                {"cut": "Talrand, Sky Summoner", "add": "Rhystic Study", "reason": "-", "evidence": "-", "expected_effect": "-"},
                {"cut": "Island", "add": "Opt", "reason": "-", "evidence": "-", "expected_effect": "-"},
                {"cut": "Island", "add": "Conterspell of Doom", "reason": "-", "evidence": "-", "expected_effect": "-"},
                {"cut": "Island", "add": "Time Warp", "reason": "One too many.", "evidence": "-", "expected_effect": "-"}]
    reviewer = FakeRunner([{"review": "## What happened\nLands.", "causes": {"deck": "All lands.", "pilot": "Fine.", "pod": "Quiet.", "variance": "None."},
                            "lessons": ["Cast the commander sooner."], "swaps": proposed}])
    result = review(deck, [report["directory"]], reviewer, database, swaps=6, echo=lambda *_: None)
    assert [(s["cut"], s["add"]) for s in result["swaps"]] == [("Island", "Counterspell")]          # the Oracle name; the seventh was over the limit
    assert len(result["refused"]) == 5 and "outside the commander's color identity" in result["refused"][0]
    assert "not legal in Commander" in result["refused"][1] and "it is the commander" in result["refused"][2]
    assert "already in the list" in result["refused"][3] and "no card named" in result["refused"][4]
    asked = reviewer.asked[0]
    assert "not the strongest deck" in asked["system"] and "6 at most" in asked["system"]
    for heading in ("THE LIST", "MEASURED FROM THE LOG", "WHAT HAPPENED TO EACH CARD THE PILOT SAW", "THE GAME, ROUND BY ROUND",
                    "WHAT THE PILOT SAID IT WAS DOING", "RULES FINDINGS ABOUT THIS SEAT", "this deck sat at seat 0 as Talrand"):
        assert heading in asked["prompt"]
    written = Path(result["review"])
    assert written == tmp_path / "talrand.review.md" and "- [ ] cut **Island**, add **Counterspell**: No interaction." in written.read_text()
    assert apply(deck, written, database)[1] == []                                                  # nothing is ticked yet
    written.write_text(written.read_text().replace("- [ ] cut **Island**", "- [x] cut **Island**"))
    assert main(["deck", "apply", str(deck), str(written), "-o", str(tmp_path / "next.txt")]) == 0
    assert (tmp_path / "next.txt").read_text() == "97 Island\n1 Counterspell\n1 Opt\n\n1 Talrand, Sky Summoner\n"
    assert deck.read_text().startswith("98 Island") and "cut Island, add Counterspell" in capsys.readouterr().out
    with pytest.raises(ValueError, match="did not play in"):
        other = tmp_path / "pirless.txt"
        other.write_text("99 Forest\n\n1 Wilson, Refined Grizzly\n", encoding="utf-8")
        review(other, [report["directory"]], reviewer, database, echo=lambda *_: None)


def test_opening_hands_are_decided_together_and_a_game_always_starts(database, tmp_path):
    budget = Budget(max_calls=300)
    seats = [ScriptedSeat(budget, mulligans=2), ScriptedSeat(budget), ScriptedSeat(budget, mulligans=2, bottoms=False)]
    report = play_pod(decks(tmp_path), seats, FakeRunner([OK] * 60, budget), database, cycles=1, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    assert report["stopped"] is None
    log = (Path(report["directory"]) / "log.md").read_text()
    assert log.count("Talrand mulligans") == 2 and "Talrand keeps 6 cards" in log and "Pir keeps 7 cards" in log
    # Thrasios never names the card it owes, in its answer or in the wake it is then given: the driver keeps for it.
    assert "Thrasios keeps 6 cards" in log
    forced = [w for w in report["wakes"] if w["forced"]]
    assert [(w["seat"], w["kind"]) for w in forced] == [(2, "mulligan")] and "chose what went on the bottom" in forced[0]["forced"]
    opening = [w for w in report["wakes"] if w["kind"] == "mulligan" and not w["tool_calls"]]
    assert sorted(w["seat"] for w in opening) == [0, 0, 0, 1, 2, 2, 2]            # three rounds of questions; Pir was asked once


def test_seats_are_asked_at_attacks_and_at_an_end_step_they_hold_for_though_the_stack_is_empty(database, tmp_path):
    budget = Budget(max_calls=600)
    seats = [ScriptedSeat(budget, cast_commander=True, attack=1), ScriptedSeat(budget, cast_commander=True, acts=True),
             ScriptedSeat(budget, holds=True, acts=True)]
    report = play_pod(decks(tmp_path), seats, FakeRunner([OK] * 150, budget), database, cycles=6, seed=4, out_root=tmp_path / "games",
                      echo=lambda *_: None)
    assert report["stopped"] is None
    events = [json.loads(line) for line in (Path(report["directory"]) / "events.jsonl").read_text().splitlines()]
    attack_turns = sorted({e["turn"] for e in events if e["tool"] == "declare_attackers" and e["args"]["attacks"]})
    asked = [t for t in report["triage"] if t["about"] == "declare_attackers window"]
    assert len(attack_turns) == 2 and sorted((t["turn"], t["seat"]) for t in asked) == [(turn, seat) for turn in attack_turns for seat in (1, 2)]
    question = next(p for p in seats[1].prompts if "Attackers have been declared" in p)
    assert "Talrand, Sky Summoner at Pir. Blockers have not been chosen yet." in question and "the stack is empty" in question
    # Both said they would act, so each was woken with priority in Talrand's declare attackers step, once.
    woken = [(w["seat"], w["turn"]) for w in report["wakes"] if w["kind"] == "priority" and w["turn"] in attack_turns and w["seat"] != 0]
    assert sorted(woken) == [(seat, turn) for seat in (1, 2) for turn in attack_turns]
    # Thrasios holds for the end step of the turn before its own, which is Pir's: asked there every round, and nowhere else.
    held = [t for t in report["triage"] if t["about"] == "end window"]
    assert {t["seat"] for t in held} == {2} and len(held) >= 4
    assert all(events_turn_active(events, t["turn"]) == 1 for t in held)


def events_turn_active(events: list[dict], turn: int) -> int:
    """Whose turn a table turn was: the seat that played a land or passed first in it."""
    return next(e["seat"] for e in events if e["turn"] == turn and e["seat"] is not None and e["step"] in ("upkeep", "main1"))


def test_a_turn_that_is_only_a_basic_land_is_not_sent_to_the_auditor(database, tmp_path):
    budget = Budget(max_calls=200)
    auditor = FakeRunner([OK] * 20, budget)
    report = play_pod(decks(tmp_path), [ScriptedSeat(budget), ScriptedSeat(budget, fidgets=True), ScriptedSeat(budget)], auditor, database,
                      cycles=2, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    assert report["stopped"] is None and len(report["audits"]) == 2 == len(auditor.asked)       # Pir's two turns, and nobody else's
    assert all("Pir acts: Tapping and untapping" in a["prompt"] for a in auditor.asked)


def test_a_player_spoken_to_by_name_answers_before_the_speaker_acts(database, tmp_path):
    budget = Budget(max_calls=200)
    seats = [ScriptedSeat(budget, asks="Pir, Thrasios: truce until Talrand's fliers are dealt with?"), ScriptedSeat(budget, answers="Deal, for two turns."),
             ScriptedSeat(budget, answers="")]
    report = play_pod(decks(tmp_path), seats, FakeRunner([OK] * 20, budget), database, cycles=1, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    assert report["stopped"] is None
    assert seats[0].heard == [[{"from": "Pir", "seat": 1, "text": "Deal, for two turns."}]]          # Thrasios stayed silent
    log = (Path(report["directory"]) / "log.md").read_text()
    assert log.index("Talrand (to Pir, Thrasios): Pir, Thrasios: truce") < log.index("Pir (to Talrand): Deal, for two turns.")
    assert [(t["seat"], t["reply"]) for t in report["talk"]] == [(1, "Deal, for two turns."), (2, "")]
    from fcsim.play.status import render, status
    assert "2 questions at the table (1 answered)" in render(status(report["directory"]))
    asked = next(p for p in seats[1].prompts if "has just said to you" in p)
    assert "You are not acting now, only talking." in asked and "YOUR HAND" in asked


def test_several_games_are_reported_side_by_side_with_their_sample_size(combat_game, capsys):
    from fcsim.cli import main
    from fcsim.play.aggregate import aggregate

    directory = combat_game[0]["directory"]
    report = aggregate([directory, directory])
    assert len(report["games"]) == 2 and report["verdicts"]["length"] == {"unfinished": 2} and report["verdicts"]["participation"] == {"bad": 2}
    talrand = report["decks"]["Talrand"]
    assert (talrand["games"], talrand["wins"], talrand["damage_to_players"], talrand["spells"]) == (2, 0, 8, 2)
    assert report["cards"]["Talrand"]["Talrand, Sky Summoner"] == {"seen": 2, "played": 2, "rounds_held": 0, "stuck_at_end": 0}
    assert main(["report", directory, "--deck", "thras"]) == 0
    out = capsys.readouterr().out
    assert "1 game(s). With so few" in out and "CARDS Thrasios saw" in out and "CARDS Talrand saw" not in out


def test_several_games_of_one_pod_are_child_processes_with_their_own_seeds(combat_game, monkeypatch, capsys):
    import subprocess

    from fcsim import cli

    commands = []

    def fake_run(command, **_):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=f"...\nEverything is in {combat_game[0]['directory']}\n", stderr="")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli, "_stay_awake", lambda: None)
    assert cli.main(["pod", "a.txt", "b.txt", "--games", "3", "--parallel", "2", "--seed", "10", "--cycles", "8", "--effort", "medium"]) == 0
    tails = sorted(command[3:] for command in commands)
    assert tails == [["pod", "a.txt", "b.txt", "--cycles", "8", "--effort", "medium", "--seed", str(seed)] for seed in (10, 11, 12)]
    out = capsys.readouterr().out
    assert "game 3 of 3 (seed 12)" in out and "3 game(s). With so few" in out and "RUBRIC  how many games got each verdict" in out
