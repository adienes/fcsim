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
                 acts: bool = False, holds: bool = False, fidgets: bool = False, asks: str = "", answers: str = "",
                 blinks: bool = False, policy: str = "", triggers: dict | None = None):
        self.budget, self.prompts, self.cast_commander, self.attack, self.contests = budget, [], cast_commander, attack, contests
        self.disputes, self.mulligans, self.bottoms, self.acts, self.holds = disputes, mulligans, bottoms, acts, holds
        self.asks, self.answers, self.heard = asks, answers, []   # a question for seat 1, once; what this seat says when spoken to
        self.triggers = triggers  # how it answers the question about its waiting triggers: {"action": ..., "script": ...}
        self.policy = policy    # a standing response policy, set again on each of its own turns (the table resets it there)
        self.blinks = blinks    # once, returns its commander to the command zone, to be cast again
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
            elif "triggers" in (schema or {}).get("properties", {}):
                handles = re.findall(r"^- \[(t\d+)\]", prompt, re.M)
                data = {"triggers": [{"pending": h, "because": "scripted", **self.triggers} for h in handles]} if self.triggers else {"triggers": []}
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
            if self.policy and waiting_for == "priority" and view["turn"]["active"] == me and view["seats"][me]["response_policy"] != self.policy:
                tool("set_response_policy", policy=self.policy)
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
            elif self.blinks and waiting_for == "priority" and view["turn"]["active"] == me and view["turn"]["step"] == "main1" and not view["stack"] \
                    and any(c.get("is_commander") and c["controller"] == me and not c["entered_this_turn"] for c in view["battlefield"]):
                self.blinks = False
                mine = next(c["handle"] for c in view["battlefield"] if c.get("is_commander") and c["controller"] == me)
                result = tool("act", because="A made-up effect returns my commander to the command zone.", mutations=[{"op": "move", "obj": mine, "to": "command"}])
            elif self.cast_commander and waiting_for == "priority" and view["turn"]["active"] == me and not view["stack"] \
                    and view["turn"]["step"] == "main1" and view["seats"][me]["command"] \
                    and len([c for c in view["battlefield"] if c["controller"] == me and not c["tapped"]]) >= 4 + 2 * sum(view["seats"][me]["commander_casts"].values()):
                lands = [c["handle"] for c in view["battlefield"] if c["controller"] == me and not c["tapped"]][:4 + 2 * sum(view["seats"][me]["commander_casts"].values())]
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
    assert later and all(p.count("Pir plays Forest") == 1 for p in later)   # a land play of Pir's reaches Talrand once, never twice


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
    # Untargeted triggers are not asked about by default; these two players say they want to be ("ask_everything").
    seats = [ScriptedSeat(budget, cast_commander=True, with_trigger=True), ScriptedSeat(budget, triage=answer, policy="ask_everything"),
             ScriptedSeat(budget, triage=answer, policy="ask_everything")]
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
    seats = [ScriptedSeat(budget, cast_commander=True, with_trigger=True), ScriptedSeat(budget, triage={"policy": "pass_always"}, policy="ask_everything"),
             ScriptedSeat(budget, policy="ask_everything")]
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


def test_a_game_is_scored_against_the_rubric_from_its_log(combat_game, database, capsys):
    from fcsim.cli import main
    from fcsim.play.rubric import measure

    report = combat_game[0]
    directory = Path(report["directory"])
    scored = measure(directory)
    assert scored == report["rubric"] == json.loads((directory / "rubric.json").read_text())
    # A replay is handed what the table was told when the game was played, from the copy pinned in its directory:
    # triggers are queued by card type, so without it a rebuilt game can leave the path its journal records.
    from fcsim.play import pod
    from fcsim.play.gamedir import GameDirectory
    pinned = GameDirectory.open(directory).card_lookup()
    assert pinned("Talrand, Sky Summoner") == pod._lookup(database)("Talrand, Sky Summoner") and pinned("A card nobody played") is None
    talrand, pir, thrasios = scored["seats"]
    # Both attacks were dealt by the table itself, from the auditor's 4/4 stamp: every combat, not only the game's first.
    assert (talrand["attackers"], talrand["damage_to_players"], talrand["spells"], talrand["idle_turns"]) == (2, 8, 1, 1)
    assert pir["attacked_by_others"] == 2 and pir["blockers"] == 0 and thrasios["spells"] == 0
    assert (thrasios["turns"], thrasios["turns_from_third"], thrasios["idle_turns"]) == (6, 4, 4)   # lands and passes, all game
    verdicts = {s["measure"]: s["verdict"] for s in scored["scores"]}
    assert verdicts["length"] == "unfinished" and verdicts["participation"] == "bad" and verdicts["interaction"] == "bad"
    assert verdicts["threat rotation"] == "mixed" and "ending" not in verdicts                    # only Pir was ever leaned on
    assert "Thrasios" in next(s["finding"] for s in scored["scores"] if s["measure"] == "spotlight")
    assert "## Against the rubric" in (directory / "report.md").read_text()
    # Audited a turn at a time, the table stops only between turns: one release a turn, not one every time the pen moved.
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    releases = [e for e in events if e["tool"] == "release"]
    assert len(releases) == len({e["turn"] for e in releases}) and len(releases) >= report["turns_played"] - 1
    assert json.loads((directory / "config.json").read_text())["table"]["gated"] == "turn"
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
    # The readable log tells a run of passes in one line; the journal keeps every one of them.
    assert "passes priority" not in log and re.search(r"- `\d+-\d+` turn 1 \w+ to \w+: .+ passed priority \(\d+ passes\)\.", log)
    journal = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    assert sum(e["tool"] == "pass_priority" for e in journal) > 5 * log.count("passed priority")
    assert report["journal"] == "replays to the same game" and "The journal replays to the same game." in (directory / "report.md").read_text()
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
    draft = {"review": "## What happened\nStuck on two lands all game.", "causes": {"deck": "All lands.", "pilot": "Fine.", "pod": "Quiet.", "variance": "None."},
             "lessons": ["Cast the commander sooner."], "swaps": proposed}
    wrong = {"claim": "Stuck on two lands all game.", "record": "The turn-by-turn lines show a land played on every turn."}
    reviewer = FakeRunner([draft, {"problems": [wrong]}, {**draft, "review": "## What happened\nLands, one a turn."}])
    result = review(deck, [report["directory"]], reviewer, database, swaps=6, echo=lambda *_: None)
    # The draft is checked against the record by a second pass, and written again when a claim does not hold.
    assert result["corrected"] == [wrong] and [a["schema"]["required"][0] for a in reviewer.asked] == ["review", "problems", "review"]
    assert "THE REVIEW TO CHECK" in reviewer.asked[1]["prompt"] and "Stuck on two lands" in reviewer.asked[1]["prompt"]
    assert "A FACT-CHECK OF THAT DRAFT FOUND\n- You wrote: Stuck on two lands all game." in reviewer.asked[2]["prompt"]
    assert [(s["cut"], s["add"]) for s in result["swaps"]] == [("Island", "Counterspell")]          # the Oracle name; the seventh was over the limit
    assert len(result["refused"]) == 5 and "outside the commander's color identity" in result["refused"][0]
    assert "not legal in Commander" in result["refused"][1] and "it is the commander" in result["refused"][2]
    assert "already in the list" in result["refused"][3] and "no card named" in result["refused"][4]
    asked = reviewer.asked[0]
    assert "not the strongest deck" in asked["system"] and "6 at most" in asked["system"]
    assert "round 4 (table turn 12): 3 lands; played Island; cast Talrand, Sky Summoner; attacked with 0" in asked["prompt"]
    assert "instants with mana value 2 or less: 1 (Opt)" in asked["prompt"] and "100 cards; 98 lands" in asked["prompt"]
    for heading in ("THE LIST", "THE LIST IN NUMBERS", "THIS SEAT TURN BY TURN", "MEASURED FROM THE LOG", "WHAT HAPPENED TO EACH CARD THE PILOT SAW", "THE GAME, ROUND BY ROUND",
                    "WHAT THE PILOT SAID IT WAS DOING", "RULES FINDINGS ABOUT THIS SEAT", "this deck sat at seat 0 as Talrand"):
        assert heading in asked["prompt"]
    written = Path(result["review"])
    assert written == tmp_path / "talrand.review.md" and "- [ ] cut **Island**, add **Counterspell**: No interaction." in written.read_text()
    assert "Lands, one a turn." in written.read_text() and "## Fact check" in written.read_text() and "- Draft: Stuck on two lands" in written.read_text()
    assert apply(deck, written, database)[1] == []                                                  # nothing is ticked yet
    written.write_text(written.read_text().replace("- [ ] cut **Island**", "- [x] cut **Island**"))
    assert main(["deck", "apply", str(deck), str(written), "-o", str(tmp_path / "next.txt")]) == 0
    assert (tmp_path / "next.txt").read_text() == "97 Island\n1 Counterspell\n1 Opt\n\n1 Talrand, Sky Summoner\n"
    assert deck.read_text().startswith("98 Island") and "cut Island, add Counterspell" in capsys.readouterr().out
    with pytest.raises(ValueError, match="did not play in"):
        other = tmp_path / "pirless.txt"
        other.write_text("99 Forest\n\n1 Wilson, Refined Grizzly\n", encoding="utf-8")
        review(other, [report["directory"]], reviewer, database, echo=lambda *_: None)
    sound = FakeRunner([draft, {"problems": []}])
    assert review(deck, [report["directory"]], sound, database, out=tmp_path / "second.md", echo=lambda *_: None)["corrected"] == []
    assert "found nothing to correct" in (tmp_path / "second.md").read_text() and len(sound.asked) == 2


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


def test_what_the_table_verified_and_needs_nothing_written_is_not_sent_to_the_auditor(database):
    from fcsim.play.pod import _needs_nothing_written
    assert _needs_nothing_written(database.find("Island"), {}) and _needs_nothing_written(database.find("Command Tower"), {})
    assert not _needs_nothing_written(database.find("Talrand, Sky Summoner"), {})                 # "Whenever you cast ..." wants a hook
    assert not _needs_nothing_written(database.find("Evolving Wilds"), {}) and not _needs_nothing_written(database.find("Mystic Sanctuary"), {})
    patterned = {"Talrand, Sky Summoner": [{"kind": "event_trigger", "text": "...", "on": {"event": "cast", "who": "you", "type": "Instant"}}]}
    assert _needs_nothing_written(database.find("Talrand, Sky Summoner"), patterned)             # known, and the table can queue it
    text_only = {"Talrand, Sky Summoner": [{"kind": "event_trigger", "text": "Whenever you cast your second spell each turn, ..."}]}
    assert not _needs_nothing_written(database.find("Talrand, Sky Summoner"), text_only)


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


def test_several_games_are_reported_side_by_side_with_their_sample_size(combat_game, capsys, tmp_path):
    from fcsim.cli import main
    from fcsim.play.aggregate import aggregate

    directory = combat_game[0]["directory"]
    report = aggregate([directory, directory])
    assert len(report["games"]) == 2 and report["verdicts"]["length"] == {"unfinished": 2} and report["verdicts"]["participation"] == {"bad": 2}
    talrand = report["decks"]["Talrand"]
    assert (talrand["games"], talrand["wins"], talrand["damage_to_players"], talrand["spells"]) == (2, 0, 16, 2)
    assert report["cards"]["Talrand"]["Talrand, Sky Summoner"] == {"seen": 2, "played": 2, "rounds_held": 0, "stuck_at_end": 0}
    assert main(["report", directory, "--deck", "thras"]) == 0
    out = capsys.readouterr().out
    assert "1 game(s). With so few" in out and "CARDS Thrasios saw" in out and "CARDS Talrand saw" not in out
    broken = aggregate([directory, str(tmp_path / "no-such-game")])   # one game that cannot be read does not cost the report of the others
    assert len(broken["games"]) == 1 and broken["unread"][0]["directory"] == "no-such-game"
    from fcsim.play.aggregate import render as render_report
    assert "COULD NOT BE READ and is in none of the numbers below" in render_report(broken)
    pace = report["games"][0]["pace"]   # in the models' own time, so games played side by side compare
    assert pace["table_turns"] >= 1 and pace["seconds_per_turn"] == round(pace["model_seconds"] / pace["table_turns"]) and "PACE" in out


def test_several_games_of_one_pod_are_child_processes_with_their_own_seeds(combat_game, monkeypatch, capsys, tmp_path):
    import subprocess

    from fcsim import cli

    commands = []

    def fake_run(command, stdout=None, **_):
        commands.append(command)
        if command[3] == "report":   # the closing report is a child too, on the code as it is on disk by then
            assert cli.main(command[3:]) == 0
            return subprocess.CompletedProcess(command, 0, stdout=capsys.readouterr().out, stderr="")
        stdout.write(f"turn 1 main1: ...\nEverything is in {combat_game[0]['directory']}\n")   # a child writes its progress to its own log
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli, "_stay_awake", lambda: None)
    out = str(tmp_path / "games")
    assert cli.main(["pod", "a.txt", "b.txt", "--games", "3", "--parallel", "2", "--seed", "10", "--cycles", "8", "--effort", "medium", "--out", out]) == 0
    tails = sorted(command[3:] for command in commands if command[3] == "pod")
    assert tails == [["pod", "a.txt", "b.txt", "--cycles", "8", "--effort", "medium", "--out", out, "--seed", str(seed)] for seed in (10, 11, 12)]
    assert [command[3:] for command in commands if command[3] == "report"] == [["report"] + [combat_game[0]["directory"]] * 3]
    assert sorted(p.name for p in next((tmp_path / "games").glob("batch_*")).iterdir()) == ["game-1.log", "game-2.log", "game-3.log"]
    out = capsys.readouterr().out
    assert "game 3 of 3 (seed 12)" in out and "3 game(s). With so few" in out and "RUBRIC  how many games got each verdict" in out


def test_a_card_the_auditor_has_seen_gets_its_hooks_the_moment_it_comes_back(database, tmp_path):
    class HooksOnce(FakeRunner):
        def ask(self, system, prompt, *, schema=None, **_):
            seen = re.search(r"THE TABLE NOW.*Talrand, Sky Summoner \[(c\d+)\]", prompt, re.S)
            if seen and not getattr(self, "wrote", False):
                self.wrote = True
                self.answers.insert(0, {**OK, "hooks": [{"kind": "event_trigger", "handle": seen[1], "source": "Talrand, Sky Summoner", "controller": 0,
                                                       "text": "Whenever you cast an instant or sorcery spell, create a 2/2 blue Drake with flying.",
                                                       "on": {"event": "cast", "who": "you", "type": "Instant"}}]})
            return super().ask(system, prompt, schema=schema)

    budget = Budget(max_calls=500)
    seats = [ScriptedSeat(budget, cast_commander=True, blinks=True), ScriptedSeat(budget), ScriptedSeat(budget)]
    report = play_pod(decks(tmp_path), seats, HooksOnce([OK] * 150, budget), database, cycles=7, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    assert report["stopped"] is None and report["standings"][0]["commander_casts"] == {"Talrand, Sky Summoner": 2}   # the second time with its tax paid
    events = [json.loads(line) for line in (Path(report["directory"]) / "events.jsonl").read_text().splitlines()]
    written = [e for e in events if e["tool"] == "hooks" and any("Talrand, Sky Summoner: Whenever you cast" in line for line in e["public"])]
    recast = [e for e in events if e["tool"] == "cast" and e["seat"] == 0][1]
    assert len(written) == 2 and written[1]["turn"] == recast["turn"] and written[1]["step"] == "main1"   # at once, not at that turn's audit
    state = json.loads((Path(report["directory"]) / "state.json").read_text())
    assert [(entry["kind"], entry["controller"], entry["on"]["who"]) for entry in state["ledger"]] == [("event_trigger", 0, "you")]
    # What one game's auditor wrote is kept, relative to the controller, and the next game has it from the card's first arrival.
    library = tmp_path / "hooks.json"
    first = play_pod(decks(tmp_path), [ScriptedSeat(budget, cast_commander=True), ScriptedSeat(budget), ScriptedSeat(budget)],
                     HooksOnce([OK] * 150, budget), database, cycles=5, seed=4, out_root=tmp_path / "games", echo=lambda *_: None, hook_library=library)
    kept = json.loads(library.read_text())
    assert kept["Talrand, Sky Summoner"][0]["controller"] == "its controller" and first["stopped"] is None
    never = FakeRunner([OK] * 150, budget)   # this game's auditor writes no hooks at all
    second = play_pod(decks(tmp_path), [ScriptedSeat(budget, cast_commander=True), ScriptedSeat(budget), ScriptedSeat(budget)],
                      never, database, cycles=5, seed=9, out_root=tmp_path / "games", echo=lambda *_: None, hook_library=library)
    ledger = json.loads((Path(second["directory"]) / "state.json").read_text())["ledger"]
    assert [(entry["kind"], entry["on"]["event"]) for entry in ledger] == [("event_trigger", "cast")]
    # A known card's own "when this enters" is put in front of its controller as it arrives: the table had matched
    # the event before the hooks existed.
    kept["Talrand, Sky Summoner"].append({"kind": "event_trigger", "controller": "its controller", "text": "When Talrand enters, scry 1.",
                                         "on": {"event": "enters", "who": "you", "self": True}})
    library.write_text(json.dumps(kept))
    third = play_pod(decks(tmp_path), [ScriptedSeat(budget, cast_commander=True), ScriptedSeat(budget), ScriptedSeat(budget)],
                     FakeRunner([OK] * 150, budget), database, cycles=5, seed=9, out_root=tmp_path / "games", echo=lambda *_: None, hook_library=library)
    log = (Path(third["directory"]) / "log.md").read_text()
    assert log.count("A trigger is waiting for Talrand: When Talrand enters, scry 1. (because Talrand, Sky Summoner entered)") == 1
    assert log.count("Talrand puts a trigger on the stack") == 1 and "Pir plays Forest" in log and "does not put a trigger" not in log


def test_a_hook_that_plainly_triggers_on_its_own_permanent_is_marked_so():
    from fcsim.play.pod import _own_trigger
    moth = _own_trigger("The Wise Mothman", {"kind": "event_trigger", "on": {"event": "enters"}, "text": "Whenever The Wise Mothman enters or attacks, each player gets a rad counter."})
    assert moth["on"] == {"event": "enters", "who": "you", "self": True}
    bog = _own_trigger("Bojuka Bog", {"kind": "event_trigger", "text": "When this land enters, exile target player's graveyard."})
    assert bog["on"] == {"event": "enters", "who": "you", "self": True}
    laelia = _own_trigger("Laelia, the Blade Reforged", {"kind": "event_trigger", "text": "Whenever Laelia attacks, exile the top card of your library."})
    assert laelia["on"]["event"] == "attacks" and laelia["on"]["self"]
    pip = _own_trigger("Pip-Boy 3000", {"kind": "event_trigger", "on": {"event": "attacks", "who": "any"}, "text": "Whenever equipped creature attacks, choose one — ..."})
    assert pip["on"] == {"event": "attacks", "attached": True}   # not "anyone's creature attacks"
    anthem = {"kind": "event_trigger", "on": {"event": "enters", "who": "you", "type": "Dragon"}, "text": "Whenever a Dragon you control enters, it deals damage."}
    assert _own_trigger("Lathliss, Dragon Queen", anthem) == anthem and _own_trigger("X", {"kind": "static", "text": "When this enters"}) == {"kind": "static", "text": "When this enters"}


def test_the_seating_can_be_drawn_from_the_seed_as_well_as_who_goes_first(database, tmp_path):
    seatings = set()
    for seed in (1, 2, 3, 4, 5, 6):
        budget = Budget(max_calls=60)
        report = play_pod(decks(tmp_path), [ScriptedSeat(budget) for _ in range(3)], FakeRunner([OK] * 20, budget), database, cycles=1, seed=seed,
                          out_root=tmp_path / "games", echo=lambda *_: None, shuffle_seats=True)
        seatings.add(tuple(report["seats"]))
        config = json.loads((Path(report["directory"]) / "config.json").read_text())
        assert [Path(d).stem.capitalize() for d in config["decks"]] == report["seats"]          # each deck sits where its name does
    assert len(seatings) >= 3 and all(sorted(s) == ["Pir", "Talrand", "Thrasios"] for s in seatings)
    again = play_pod(decks(tmp_path), [ScriptedSeat(Budget(max_calls=60)) for _ in range(3)], FakeRunner([OK] * 20), database, cycles=1, seed=3,
                     out_root=tmp_path / "games", echo=lambda *_: None, shuffle_seats=True)
    assert tuple(again["seats"]) in seatings                                                        # a seed repeats its seating


def test_an_untargeted_trigger_is_not_asked_about_unless_a_player_wants_everything(database, tmp_path):
    budget = Budget(max_calls=400)
    seats = [ScriptedSeat(budget, cast_commander=True, with_trigger=True), ScriptedSeat(budget), ScriptedSeat(budget, policy="ask_everything")]
    report = play_pod(decks(tmp_path), seats, FakeRunner([OK] * 120, budget), database, cycles=5, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    about = [(t["seat"], t["about"]) for t in report["triage"]]
    assert (2, "a triggered ability") in about and (1, "a triggered ability") not in about        # Pir is asked about the spell only
    assert (1, "Talrand, Sky Summoner") in about and (2, "Talrand, Sky Summoner") in about


def test_a_script_is_trusted_only_when_the_card_the_vocabulary_and_two_audits_all_agree(database):
    from fcsim.play.pod import _absolute, _script_trusted
    scry = [{"op": "draw", "who": "you", "count": 1}]
    talrand = database.find("Talrand, Sky Summoner")   # "Whenever you cast an instant or sorcery spell, create a 2/2 blue Drake creature token with flying."
    drake = [{"op": "create_token", "token": {"name": "Drake", "types": "Creature — Drake", "power": 2, "toughness": 2, "text": "Flying"}, "count": 1}]
    hook = {"kind": "event_trigger", "text": "Whenever you cast an instant or sorcery spell, create a 2/2 blue Drake creature token with flying.",
            "on": {"event": "cast", "who": "you", "type": "Instant"}, "script": drake}
    assert not _script_trusted(talrand, {**hook, "script_seen": 1})                         # written once: not yet
    assert _script_trusted(talrand, {**hook, "script_seen": 2})                             # twice, a plain effect, the table's own operations
    assert not _script_trusted(talrand, {**hook, "script_seen": 5, "script_revoked": True})  # one finding against it, and never again
    assert not _script_trusted(talrand, {**hook, "script_seen": 2, "script": [{"op": "shuffle"}]})
    assert not _script_trusted(talrand, {**hook, "script_seen": 2, "text": "Whenever a land enters, draw a card."})   # not what the card says
    rhystic = database.find("Rhystic Study")           # "... you may draw a card unless that player pays {1}."
    assert not _script_trusted(rhystic, {"kind": "event_trigger", "text": "Whenever an opponent casts a spell, you may draw a card unless that player pays {1}.",
                                         "on": {"event": "cast", "who": "opponent"}, "script": scry, "script_seen": 9})
    made = _absolute([{**hook, "controller": "its controller", "script_seen": 2}], type("P", (), {"controller": 2, "name": "Talrand, Sky Summoner"})(), database)
    assert made[0]["scripted"] is True and made[0]["controller"] == 2 and "script_seen" not in made[0]


def test_a_script_earns_its_trust_over_two_audits_and_is_then_left_alone(database, tmp_path):
    drake = [{"op": "create_token", "token": {"name": "Drake", "types": "Creature — Drake", "power": 2, "toughness": 2, "text": "Flying"}, "count": 1}]

    class Describes(FakeRunner):
        """Writes Talrand's hook, script included, whenever the board shows Talrand without "(hooks written)"."""
        def ask(self, system, prompt, *, schema=None, **_):
            line = re.search(r"Talrand, Sky Summoner \[(c\d+)\][^\n]*", prompt.split("THE TABLE NOW")[-1]) if "THE TABLE NOW" in prompt else None
            if line and "(hooks written)" not in line[0]:
                self.answers.insert(0, {**OK, "hooks": [{"kind": "event_trigger", "handle": line[1], "source": "Talrand, Sky Summoner", "controller": 0, "script": drake,
                                                       "text": "Whenever you cast an instant or sorcery spell, create a 2/2 blue Drake creature token with flying.",
                                                       "on": {"event": "cast", "who": "you", "type": "Instant"}}]})
                self.wrote = getattr(self, "wrote", 0) + 1
            return super().ask(system, prompt, schema=schema)

    library, states = tmp_path / "hooks.json", []
    for seed in (4, 9, 5):
        budget = Budget(max_calls=400)
        auditor = Describes([OK] * 150, budget)
        report = play_pod(decks(tmp_path), [ScriptedSeat(budget, cast_commander=True), ScriptedSeat(budget), ScriptedSeat(budget)], auditor, database,
                          cycles=6, seed=seed, out_root=tmp_path / "games", echo=lambda *_: None, hook_library=library)
        ledger = json.loads((Path(report["directory"]) / "state.json").read_text())["ledger"]
        kept = json.loads(library.read_text())["Talrand, Sky Summoner"][0]
        states.append((auditor.wrote if hasattr(auditor, "wrote") else 0, kept["script_seen"], [e.get("scripted") for e in ledger]))
        assert report["journal"] == "replays to the same game"
    # game 1: described once, not trusted. game 2: described again, the same, and trusted from then. game 3: trusted on arrival, and not described at all.
    assert states == [(1, 1, [False]), (1, 2, [True]), (0, 2, [True])]


@pytest.mark.parametrize("answer, life, stacked", [({"action": "script", "script": [{"op": "life", "who": "you", "delta": 1}]}, 41, True),
                                                   ({"action": "decline"}, 40, False), ({"action": "wake"}, 40, True),
                                                   ({"action": "script", "script": [{"op": "shuffle"}]}, 40, True)])
def test_a_waiting_trigger_is_settled_with_one_question_where_it_can_be(database, tmp_path, answer, life, stacked):
    budget = Budget(max_calls=200)
    seats = [ScriptedSeat(budget, fidgets=True), ScriptedSeat(budget, fidgets=True), ScriptedSeat(budget, fidgets=True, triggers=answer)]
    trigger = {"kind": "trigger", "seat": 2, "summary": "Thrasios gains 1 life", "basis": "test", "fix": "put it on the stack", "confidence": "high"}
    report = play_pod(decks(tmp_path), seats, FakeRunner([{"verdict": "findings", "findings": [trigger], "hooks": []}] + [OK] * 40, budget), database,
                      cycles=2, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    assert report["stopped"] is None and report["standings"][2]["life"] == life and report["journal"] == "replays to the same game"
    woken = [w for w in report["wakes"] if w["kind"] == "triggers"]
    log = (Path(report["directory"]) / "log.md").read_text()
    if answer["action"] == "script" and answer["script"][0]["op"] == "life":
        assert woken == [] and "to resolve by the script they gave" in log and "resolves by script: Thrasios is at 41 life." in log
    elif answer["action"] == "decline":
        assert woken == [] and "Thrasios does not put a trigger on the stack" in log
    else:   # it asked to be woken, or gave a script the table will not run: a session as before
        assert len(woken) == 1
    assert ("Thrasios puts a trigger on the stack" in log) == stacked
    asked = [t for t in report["triage"] if t["about"] == "its waiting triggers"]
    assert len(asked) == 1 and asked[0]["seat"] == 2


def test_the_driver_is_an_object_whose_pieces_can_be_tried_without_playing_a_game(database, tmp_path):
    from fcsim.cards import read_decklist, resolve
    from fcsim.play.gamedir import GameDirectory
    from fcsim.play.pod import _lookup, _Pod
    from fcsim.table import Table

    lists = [resolve(read_decklist(path), database) for path in decks(tmp_path)]
    names = ["Talrand", "Pir", "Thrasios"]
    directory = GameDirectory(tmp_path / "games", "pod_by-hand", 4)
    table = Table([{"name": n, "commanders": [c.name for c in d.commanders], "library": [c.name for c in d.library()]} for n, d in zip(names, lists, strict=True)],
                  seed=4, gated="turn", on_event=directory.journal, card_lookup=_lookup(database))
    budget = Budget(max_calls=20)
    pod = _Pod(directory, table, names, lists, [ScriptedSeat(budget) for _ in names], FakeRunner([OK] * 5, budget), database, 1, None, lambda *_: None)
    for seat in range(3):
        table.act(seat, "keep")
    first = table.game.turn.active
    assert table.game.turn.step == "upkeep" and pod.settle() == "wake"            # the quiet upkeep and draw step were passed through with no model
    assert table.game.turn.step == "main1" and table.expected() == [(first, "priority")] and pod.holds_pen(first) and budget.calls == 0
    told = pod.briefing(first)
    assert "WHAT YOUR UNTAPPED MANA CAN PAY FOR" in told and "YOUR HAND" in told and pod.when().startswith(f"Round 1, {names[first]}'s turn")
    assert pod.news(first).startswith("Since your last wake:") or pod.news(first).startswith("Nothing has happened")


def test_a_bug_in_the_driver_stops_the_game_where_it_can_be_resumed_instead_of_losing_it(database, tmp_path, monkeypatch):
    from fcsim.play import pod
    budget = Budget(max_calls=100)
    calls = {"n": 0}

    def breaks_once(table):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyError("a planeswalker the arithmetic did not expect")
        return []
    monkeypatch.setattr(pod, "_must_lose", breaks_once)
    report = play_pod(decks(tmp_path), [ScriptedSeat(budget) for _ in range(3)], FakeRunner([OK] * 20, budget), database, cycles=2, seed=4,
                      out_root=tmp_path / "games", echo=lambda *_: None)
    directory = Path(report["directory"])
    assert report["stopped"].startswith("internal error: KeyError") and "Traceback" in (directory / "error.txt").read_text()
    assert (directory / "report.md").is_file() and report["journal"] == "replays to the same game"
    monkeypatch.undo()
    resumed = play_pod([], [ScriptedSeat(budget) for _ in range(3)], FakeRunner([OK] * 20, budget), database, cycles=1, resume=directory, echo=lambda *_: None)
    assert resumed["stopped"] is None and resumed["turns_played"] >= 3                     # the journal had everything: the game goes on


def test_a_deck_project_teaches_its_pilot_what_earlier_games_learned(combat_game, database, tmp_path):
    from fcsim.cli import main
    from fcsim.workshop.learn import learn
    from fcsim.workshop.project import DeckProject, deck_argument

    source = tmp_path / "talrand.txt"
    source.write_text("99 Island\n\n1 Talrand, Sky Summoner\n", encoding="utf-8")
    assert main(["deck", "init", str(source), "--name", "talrand drakes", "--root", str(tmp_path / "decks")]) == 0
    project = DeckProject(tmp_path / "decks" / "talrand-drakes")
    assert project.primer() == "" and project.deck_id == "talrand-drakes"
    assert deck_argument(project.path)[0] == project.list_path and deck_argument(project.path)[1].path == project.path and deck_argument(source) == (source, None)

    game = combat_game[0]["directory"]
    lesson = {"claim": "Cast Talrand, Sky Summoner as soon as you have four lands; attack from the turn after.", "kind": "commander_timing",
              "cards": ["Talrand, Sky Summoner"], "evidence": "Round 4: cast on four Islands; rounds 5 and 6: attacked Pir."}
    invented = {"claim": "Hold Counterspell for Pir's commander.", "kind": "interaction", "cards": ["Counterspell"], "evidence": "Round 3: Counterspell was cast."}
    teacher = FakeRunner([{"lessons": [lesson, invented]}, {"problems": [{"claim": "Hold Counterspell for Pir's commander.", "record": "No Counterspell is in the list."}]}])
    teacher.ask_text = None

    class Writes(FakeRunner):
        def ask(self, system, prompt, *, schema=None, **_):
            if schema is None:   # the primer is prose, not JSON
                self.asked.append({"system": system, "prompt": prompt, "schema": None})
                return Reply(data=None, text="## Gameplan\nCast Talrand on four lands and attack.", cost_usd=0.01, seconds=0.1, model="fake")
            return super().ask(system, prompt, schema=schema)
    teacher = Writes(teacher.answers)
    first = learn(project.path, [game], teacher, database, echo=lambda *_: None)
    assert (first["added"], first["validated"], first["dropped"]) == (1, 0, 1)                      # the invented lesson never reaches the file
    kept = project.lessons()
    assert [(k["id"], k["status"], k["games"], k["stale"]) for k in kept] == [("L001", "tentative", [Path(game).name], False)]
    assert "Cast Talrand on four lands" in project.primer() and "studied 1 game(s)" in (project.path / "CHANGELOG.md").read_text()
    assert "[tentative, 1 game(s), commander_timing]" in teacher.asked[-1]["prompt"]
    studied = teacher.asked[0]["prompt"]   # the learner starts from how the seat was piloted, which nobody has to guess at
    assert "HOW THIS SEAT WAS PILOTED, MEASURED" in studied and "opening hand kept after 0 mulligan(s):" in studied and "Start from HOW THIS SEAT WAS PILOTED" in teacher.asked[0]["system"]

    # the same games studied again are not a second witness; a different game is, and the lesson becomes validated
    again = Writes([{"lessons": [lesson]}, {"problems": []}])
    assert learn(project.path, [game], again, database, echo=lambda *_: None)["validated"] == 0
    class Rambles(Writes):   # a primer over its limit is asked for once more, with the count
        def ask(self, system, prompt, *, schema=None, **_):
            if schema is None and "THE PRIMER YOU WROTE" not in prompt:
                self.asked.append({"system": system, "prompt": prompt, "schema": None})
                return Reply(data=None, text="## Gameplan\n" + "word " * 700, cost_usd=0.01, seconds=0.1, model="fake")
            return super().ask(system, prompt, schema=schema)
    long = Rambles([{"lessons": [lesson]}, {"problems": []}])
    assert learn(project.path, [game], long, database, echo=lambda *_: None)["primer_words"] < 20 and "It is 702 words and may be 600" in long.asked[-1]["prompt"]
    assert "Cast Talrand on four lands" in project.primer()

    # learned from another game it comes back in other words: whoever wrote it was shown the record and says which lesson it restates
    reworded = {"claim": "Get the commander down early, as soon as the fourth land is there.", "kind": "commander_timing", "cards": ["Talrand, Sky Summoner"],
                "evidence": "another game", "same_as": "L001"}
    assert "LESSONS ALREADY RECORDED" in again.asked[0]["prompt"] and "- L001 [tentative" in again.asked[0]["prompt"]
    assert project.record([reworded], games=["another-game"], database=database) == {"added": 0, "validated": 1, "total": 1}
    assert project.lessons()[0]["status"] == "validated" and project.lessons()[0]["games"] == [Path(game).name, "another-game"]

    # the next game's pilot is told, and the game keeps exactly what it was told
    budget = Budget(max_calls=60)
    seats = [ScriptedSeat(budget) for _ in range(3)]
    others = decks(tmp_path)[1:]
    report = play_pod([project.path, *others], seats, FakeRunner([OK] * 20, budget), database, cycles=1, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    directory = Path(report["directory"])
    assert (directory / "decks" / "seat-0.primer.md").read_text().startswith("## Gameplan") and not (directory / "decks" / "seat-1.primer.md").exists()
    config = json.loads((directory / "config.json").read_text())
    assert config["projects"][0]["deck_id"] == "talrand-drakes" and config["projects"][1] is None and len(config["projects"][0]["guidance_hash"]) == 16
    # a list revision keeps the identity and its lessons; a lesson about a card no longer in the list is flagged, not taught
    project.list_path.write_text("99 Island\n\n1 Shorikai, Genesis Engine\n", encoding="utf-8")
    project.record([], games=[], database=database)
    assert project.lessons()[0]["stale"] is True


def test_a_deck_project_can_be_given_to_the_pod_command_with_random_opponents(database, tmp_path, monkeypatch):
    from fcsim import cli
    from fcsim.workshop.project import DeckProject

    source = tmp_path / "talrand.txt"
    source.write_text("99 Island\n\n1 Talrand, Sky Summoner\n", encoding="utf-8")
    project = DeckProject.init(source, database, root=tmp_path / "decks", name="talrand")
    drawn, played = [], {}
    monkeypatch.setattr(cli, "random_commanders", lambda count, seed=None, exclude=(): drawn.append(exclude) or [])
    monkeypatch.setattr(cli, "play_pod", lambda decks, *a, **k: played.update(decks=decks) or {"directory": str(tmp_path), "stopped": "stubbed"})
    monkeypatch.setattr(cli, "CardDatabase", lambda: database)
    monkeypatch.setattr(cli, "_stay_awake", lambda: None)
    (tmp_path / "report.md").write_text("stub", encoding="utf-8")
    other = decks(tmp_path)[1]
    cli.main(["pod", str(project.path), str(other), "--random-opponents", "2", "--seed", "3"])
    assert drawn == [("Talrand, Sky Summoner", "Pir, Imaginative Rascal")] and played["decks"][0] == str(project.path)   # read from the project's list; the project goes on to the game


def test_a_finding_at_a_scripted_resolution_that_is_in_no_library_stops_nothing(database, tmp_path):
    """The second batch's second game died in the audit: a missed trigger was found at the event where a
    delayed trigger had resolved by script, and the driver looked its name up among the cards it knew."""
    budget = Budget(max_calls=200)

    class Blaming(FakeRunner):
        def ask(self, system, prompt, **more):
            found = re.search(r"(\d+)\D[^\n]*resolves by script", prompt)
            if found and not getattr(self, "blamed", False):
                self.blamed = True
                self.answers.insert(0, {"verdict": "findings", "hooks": [], "findings": [
                    {"kind": "misresolved", "event": int(found[1]), "seat": 2, "summary": "blames the scripted resolution", "basis": "test", "fix": "none needed", "confidence": "high"}]})
            return super().ask(system, prompt, **more)

    seats = [ScriptedSeat(budget, fidgets=True), ScriptedSeat(budget, fidgets=True),
             ScriptedSeat(budget, fidgets=True, triggers={"action": "script", "script": [{"op": "life", "who": "you", "delta": 1}]})]
    trigger = {"kind": "trigger", "seat": 2, "summary": "Thrasios gains 1 life", "basis": "test", "fix": "put it on the stack", "confidence": "high"}
    auditor = Blaming([{"verdict": "findings", "findings": [trigger], "hooks": []}] + [OK] * 40, budget)
    report = play_pod(decks(tmp_path), seats, auditor, database, cycles=2, seed=4, out_root=tmp_path / "games", echo=lambda *_: None,
                      hook_library=tmp_path / "hooks.json")
    assert auditor.blamed and report["stopped"] is None and not (Path(report["directory"]) / "error.txt").exists()


def test_a_game_that_stopped_inside_an_audit_is_still_owed_that_audit_when_it_is_resumed(database, tmp_path, monkeypatch):
    """The second batch's second game: the auditor found Cyclonic Rift resolved against the wrong permanent, the
    driver died before the correction, and the resume audited only what had happened since it began: nothing."""
    from fcsim.play import pod
    budget = Budget(max_calls=300)
    finding = {"kind": "misresolved", "seat": 0, "summary": "what Talrand did was wrong", "basis": "test", "fix": "put it right", "confidence": "high"}
    real, calls = pod._characteristics_print, {"n": 0}

    def breaks_once(table):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("inside the audit, after the verdict and before the correction")
        return real(table)
    monkeypatch.setattr(pod, "_characteristics_print", breaks_once)
    report = play_pod(decks(tmp_path), [ScriptedSeat(budget, fidgets=True) for _ in range(3)], FakeRunner([{"verdict": "findings", "findings": [finding], "hooks": []}] + [OK] * 20, budget),
                      database, cycles=1, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    directory = Path(report["directory"])
    assert report["stopped"].startswith("internal error") and "correct" not in (directory / "log.md").read_text().lower()
    monkeypatch.undo()
    auditor = FakeRunner([{"verdict": "findings", "findings": [finding], "hooks": []}] + [OK] * 20, budget)
    resumed = play_pod([], [ScriptedSeat(budget, fidgets=True) for _ in range(3)], auditor, database, cycles=1, resume=directory, echo=lambda *_: None)
    audits = [json.loads(line) for line in (directory / "audits.jsonl").read_text().splitlines()]
    assert resumed["stopped"] is None and audits[1]["events"][0] == audits[0]["events"][0]      # the same stretch, from the board as it stood then
    assert "what Talrand did was wrong" in (directory / "log.md").read_text()                   # and this time the correction is made
    assert resumed["journal"] == "replays to the same game"


def test_a_resumed_game_can_be_taken_up_from_an_earlier_event_and_nobody_counts_what_was_abandoned(database, tmp_path):
    from fcsim.audit.auditor import record
    from fcsim.play.gamedir import GameDirectory
    from fcsim.play.rubric import measure
    from fcsim.table import Table
    budget = Budget(max_calls=400)
    report = play_pod(decks(tmp_path), [ScriptedSeat(budget, fidgets=True) for _ in range(3)], FakeRunner([OK] * 40, budget), database, cycles=2, seed=4,
                      out_root=tmp_path / "games", echo=lambda *_: None)
    directory = Path(report["directory"])
    events = GameDirectory.open(directory).events()
    back = next(e["n"] for e in events if e["turn"] == 4 and e["tool"] == "act")   # the second round has begun, and something in it needs an audit
    lands_before = sum(1 for e in events if e["tool"] == "play_land")
    auditor = FakeRunner([OK] * 40, budget)
    resumed = play_pod([], [ScriptedSeat(budget, fidgets=True) for _ in range(3)], auditor, database, cycles=1, resume=directory, echo=lambda *_: None,
                       rewind_to=back, rewind_because="test")
    events = GameDirectory.open(directory).events()
    rewind = next(e for e in events if e["tool"] == "rewind")
    gone = Table.undone(events)
    assert gone == set(range(back + 1, rewind["n"])) and resumed["journal"] == "replays to the same game"
    table = Table.replay(json.loads((directory / "config.json").read_text())["table"], events)
    told = "\n".join(record(table, back + 1))
    assert f"event {back + 1} " not in told and "The table is rewound to just after event" in told      # the auditor is not shown the abandoned line
    assert "WHAT WAS DONE" in auditor.asked[0]["prompt"] and f"event {back} " in auditor.asked[0]["prompt"]   # the turn it went back into is audited from its start
    from fcsim.play.pilotlog import pilot_record
    for seat in pilot_record(directory)["seats"]:   # a turn the game went back into is one turn in the piloting record too
        assert len({t["turn"] for t in seat["turns"]}) == len(seat["turns"])
    played = sum(1 for e in events if e["tool"] == "play_land" and e["n"] not in gone)
    assert sum(1 for turn in measure(directory)["turns"] if turn["land"]) == played <= lands_before + 3   # the scores count each land once


def test_a_missed_trigger_written_as_text_alone_unmarks_its_card_so_the_next_audit_gives_it_a_pattern(database, tmp_path):
    from fcsim.audit.auditor import describe
    from fcsim.cards import read_decklist, resolve
    from fcsim.play.gamedir import GameDirectory
    from fcsim.play.pod import _lookup, _Pod
    from fcsim.table import Table

    lists = [resolve(read_decklist(path), database) for path in decks(tmp_path)]
    names = ["Talrand", "Pir", "Thrasios"]
    directory = GameDirectory(tmp_path / "games", "pod_missed", 4)
    table = Table([{"name": n, "commanders": [c.name for c in d.commanders], "library": [c.name for c in d.library()]} for n, d in zip(names, lists, strict=True)],
                  seed=4, first=0, gated="turn", on_event=directory.journal, card_lookup=_lookup(database))
    budget = Budget(max_calls=10)
    pod = _Pod(directory, table, names, lists, [ScriptedSeat(budget) for _ in names], FakeRunner([], budget), database, 1, None, lambda *_: None)
    for seat in range(3):
        table.act(seat, "keep")
    commander = table.view(0)["seats"][0]["command"][0]["handle"]
    table.act(0, "act", {"because": "test", "mutations": [{"op": "move", "obj": commander, "to": "battlefield"}]})
    handle = next(o["handle"] for o in table.view(None)["battlefield"])
    table.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "Whenever one or more nonland cards are milled, ..."},
                       {"kind": "static", "handle": handle, "text": "Other Wizards get +1/+1."}])
    assert "(hooks written)" in describe(table.view(None), database)
    pod.flag_missed({"kind": "trigger", "seat": 0, "summary": "A nonland card was milled, which triggers Talrand. It was never put on the stack."})
    told = describe(table.view(None), database)
    assert "(a trigger of this was missed: write its hooks again)" in told and "(hooks written)" not in told
    assert [e.get("missed") for e in table.game.ledger] == [True, None]                        # the static ability is kept as it was
    table.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "Whenever one or more nonland cards are milled, ...", "on": {"event": "milled", "nonland": True, "once": True}},
                       {"kind": "static", "handle": handle, "text": "Other Wizards get +1/+1."}])
    assert "(hooks written)" in describe(table.view(None), database) and not any(e.get("missed") for e in table.game.ledger)
    assert Table.replay(table.config, table.events, card_lookup=_lookup(database)).game.to_dict() == table.game.to_dict()


@pytest.mark.parametrize("held", [False, True])
def test_the_turn_so_far_is_audited_and_put_right_before_the_driver_removes_a_player(database, tmp_path, monkeypatch, held):
    """Nothing brings a player back. Zaxara was removed at 0 life by combat damage dealt twice; the audit a turn later had
    the damage undone, and the player stayed out."""
    from fcsim.play import pod
    budget = Budget(max_calls=200)

    def two_lose_in_the_second_turn(table):
        acted = any(e["tool"] == "act" and e["turn"] == 2 for e in table.events)
        return [(0, "test"), (2, "test")] if acted and table.game.control.kind != "over" and table.game.seats[0].status == "alive" else []
    monkeypatch.setattr(pod, "_must_lose", two_lose_in_the_second_turn)
    finding = {"kind": "misresolved", "seat": 1, "summary": "the damage was dealt twice", "basis": "test", "fix": "undo it", "confidence": "high"}
    still_here = {"kind": "sba", "seat": 0, "summary": "Talrand has lost the game and is still listed as alive", "basis": "704.5a", "fix": "remove Talrand", "confidence": "high"}
    last = {"verdict": "findings", "findings": [finding, still_here], "hooks": []} if held else {"verdict": "findings", "findings": [still_here], "hooks": []}
    auditor = FakeRunner([OK, last] + [OK] * 5, budget)
    report = play_pod(decks(tmp_path), [ScriptedSeat(budget, fidgets=True) for _ in range(3)], auditor, database, cycles=3, seed=4,
                      out_root=tmp_path / "games", echo=lambda *_: None)
    assert report["winner"] == "Pir" and report["stopped"] is None and report["journal"] == "replays to the same game"
    assert "Tapping and untapping my land" in auditor.asked[1]["prompt"]          # the turn so far, read before anybody goes
    assert "WHY YOU ARE ASKED NOW\nThe table is about to remove Talrand (test); Thrasios (test)" in auditor.asked[1]["prompt"]
    assert "still listed as alive" not in json.dumps(report["audits"]) and "correct the table: Talrand has lost" not in (Path(report["directory"]) / "log.md").read_text()
    log = (Path(report["directory"]) / "log.md").read_text()
    if held:
        assert log.index("The auditor asks Pir to correct the table: the damage was dealt twice") < log.index("Talrand is out of the game")
    assert report["ending"].startswith("stands: everything up to the last loss had been audited") and "**The ending stands" in (Path(report["directory"]) / "report.md").read_text()


def test_a_game_a_player_ended_by_hand_has_its_last_stretch_audited_and_the_report_says_what_is_held_against_it(database, tmp_path):
    from fcsim.cards import read_decklist, resolve
    from fcsim.play.gamedir import GameDirectory
    from fcsim.play.pod import _lookup, _Pod
    from fcsim.table import Table

    lists = [resolve(read_decklist(path), database) for path in decks(tmp_path)]
    names = ["Talrand", "Pir", "Thrasios"]
    directory = GameDirectory(tmp_path / "games", "pod_ended-by-hand", 4)
    table = Table([{"name": n, "commanders": [c.name for c in d.commanders], "library": [c.name for c in d.library()]} for n, d in zip(names, lists, strict=True)],
                  seed=4, first=0, gated="turn", on_event=directory.journal, card_lookup=_lookup(database))
    budget = Budget(max_calls=10)
    finding = {"kind": "misresolved", "seat": 0, "summary": "the spell that won was countered", "basis": "test", "fix": "none now", "confidence": "high"}
    auditor = FakeRunner([{"verdict": "findings", "findings": [finding], "hooks": []}], budget)
    pod = _Pod(directory, table, names, lists, [ScriptedSeat(budget) for _ in names], auditor, database, 1, None, lambda *_: None)
    for seat in range(3):
        table.act(seat, "keep")
    snapshot = pod.auditor.snapshot(table)
    while table.game.turn.step != "main1":
        table.act(table.expected()[0][0], "pass_priority")
    table.act(0, "act", {"because": "Crackle with Power for 40 each", "mutations": [{"op": "eliminate", "seat": 1, "reason": "0 life"}, {"op": "eliminate", "seat": 2, "reason": "0 life"}]})
    assert table.game.control.kind == "over"
    pod.last_audit(snapshot)
    assert pod.report["audits"][-1]["final"] and "Crackle with Power" in auditor.asked[0]["prompt"]
    assert pod.report["ending"].startswith("contested: the spell that won was countered")



def test_a_game_stopped_by_the_session_limit_can_wait_for_the_reset_and_take_itself_up_again(combat_game, monkeypatch, capsys, tmp_path):
    from fcsim import cli
    directory = combat_game[0]["directory"]
    begun = cli._rounds_begun(directory)
    calls, slept = [], []
    limit = "model error: Claude Code reported an error: You've hit your session limit · resets 3:30pm (America/New_York)"

    def fake_play(decks, *_, cycles, resume, rewind_to, **__):
        calls.append({"decks": list(decks), "cycles": cycles, "resume": resume, "rewind_to": rewind_to})
        return {"directory": directory, "stopped": limit if len(calls) == 1 else None, "seats": ["Talrand", "Pir", "Thrasios"]}
    monkeypatch.setattr(cli, "play_pod", fake_play)
    monkeypatch.setattr(cli, "CardDatabase", lambda: None)
    monkeypatch.setattr(cli, "_stay_awake", lambda: None)
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    assert cli.main(["pod", "a.txt", "b.txt", "c.txt", "--cycles", "10", "--wait-for-limit"]) == 0
    assert [c["resume"] for c in calls] == [None, directory] and calls[0]["cycles"] == 10 and calls[1]["cycles"] == 10 - (begun - 1)   # to the same round
    assert calls[1]["decks"] == ["Talrand", "Pir", "Thrasios"] and len(slept) == 1 and 120 <= slept[0] <= 24 * 3600 + 120
    assert "waiting" in capsys.readouterr().out
    calls.clear()
    assert cli.main(["pod", "a.txt", "b.txt", "c.txt", "--cycles", "10"]) == 1 and len(calls) == 1      # not asked to wait: it stops, as before


def test_how_each_seat_was_piloted_is_measured_from_the_journal(combat_game):
    """A deck that loses has not shown it is weak. What its pilot kept, left unused and held back is in the journal."""
    from fcsim.play.pilotlog import pilot_record, render_pilot
    record = pilot_record(combat_game[0]["directory"])
    talrand, pir, thrasios = record["seats"]
    assert [s["name"] for s in record["seats"]] == ["Talrand", "Pir", "Thrasios"]
    assert talrand["kept"]["mulligans"] == 0 and len(talrand["kept"]["cards"]) == 7 and talrand["kept"]["lands"] == sum(1 for c in talrand["kept"]["cards"] if c in ("Island", "Forest"))
    cast = [name for turn in talrand["turns"] for name in turn["cast"]]
    assert "Talrand, Sky Summoner" in cast and sum(t["attacked_with"] for t in talrand["turns"]) >= 1          # it cast its commander and attacked with it
    assert all(t["played_land"] == (not t.get("skipped_land", False)) for t in talrand["turns"][:2]) and talrand["turns"][0]["lands"] == 0
    quiet = thrasios["totals"]                                                                                # the seat that only plays lands
    assert quiet["turns_with_attackers_and_no_attack"] == 0 and sum(t["attacked_with"] for t in thrasios["turns"]) == 0
    assert all("untapped_sources" in t for t in pir["turns"][:-1])                                            # every finished turn says what it left untapped
    told = render_pilot(record, 0)
    assert "opening hand kept after 0 mulligan(s):" in told and "could attack with" in told and "in all:" in told


def test_the_mulligan_question_counts_the_hand_for_the_pilot(database, tmp_path):
    budget = Budget(max_calls=60)
    seats = [ScriptedSeat(budget) for _ in range(3)]
    play_pod(decks(tmp_path), seats, FakeRunner([OK] * 20, budget), database, cycles=1, seed=4, out_root=tmp_path / "games", echo=lambda *_: None)
    asked = next(p for p in seats[0].prompts if "This is your opening hand" in p)
    hand = asked.split("YOUR HAND")[1].split("\n\n")[0]
    lands = sum(1 for line in hand.splitlines() if line.startswith("- [") and ("] Island" in line or "] Forest" in line))
    assert f"Counted for you: {lands} land(s)" in asked and "the free mulligan is there to be used" in asked
    assert "Your commander costs {2}{U}{U}. This hand's mana makes U. In the whole list of 100, sources of each: U 99" in asked   # colours nobody can count at the table


def test_an_opponent_drawn_at_random_is_a_deck_project_and_every_project_that_played_can_be_taught(database, tmp_path, monkeypatch, capsys):
    """The opponents' pilots met their decks as strangers every game, and a playtest is only as good as the table it is played at."""
    from fcsim import cli
    monkeypatch.chdir(tmp_path)
    mine = decks(tmp_path)[0]
    fetched, played, taught = [], [], []
    monkeypatch.setattr(cli, "random_commanders", lambda count, seed=None, exclude=(): [{"name": "Pir, Imaginative Rascal", "slug": "pir-imaginative-rascal"}])
    monkeypatch.setattr(cli, "average_deck", lambda name, page_slug=None: fetched.append(name) or "99 Forest\n\n1 Pir, Imaginative Rascal\n")
    monkeypatch.setattr(cli, "play_pod", lambda given, *a, **k: played.append(list(given)) or {"directory": str(tmp_path), "stopped": "stubbed", "seats": []})
    monkeypatch.setattr(cli, "CardDatabase", lambda: database)
    monkeypatch.setattr(cli, "_stay_awake", lambda: None)
    (tmp_path / "report.md").write_text("stub", encoding="utf-8")
    for _ in range(2):
        cli.main(["pod", str(mine), "--random-opponents", "1", "--seed", "3"])
    project = tmp_path / "decks" / "pir-imaginative-rascal"
    assert (project / "current.txt").is_file() and (project / "primer.md").is_file() and fetched == ["Pir, Imaginative Rascal"]   # drawn again, it plays what it has
    assert [given[1] for given in played] == [str(Path("decks") / "pir-imaginative-rascal")] * 2
    games = []
    for index, sat in enumerate(([{"deck_id": "pir-imaginative-rascal"}, None], [None, {"deck_id": "pir-imaginative-rascal"}, {"deck_id": "nobody-kept-this"}])):
        games.append(tmp_path / f"game-{index}")
        games[-1].mkdir()
        (games[-1] / "config.json").write_text(json.dumps({"projects": sat}), encoding="utf-8")
    monkeypatch.setattr(cli, "learn_lessons", lambda project, game_dirs, runner, db: taught.append((project, list(game_dirs))) or {"added": 1})
    monkeypatch.setattr(cli, "make_runner", lambda *a, **k: None)
    assert cli.main(["deck", "learn", "all", *map(str, games)]) == 0
    assert taught == [(str(Path("decks") / "pir-imaginative-rascal"), [str(g) for g in games])]      # from the games it sat in; a project that is gone teaches nobody
