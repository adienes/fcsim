from __future__ import annotations

import json
from pathlib import Path

import pytest

from fcsim.agents import Budget, BudgetExceeded, FakeRunner
from fcsim.audit import VERDICT_SCHEMA, Auditor, build, load_scenarios, materials, play, run_suite, score
from fcsim.cards import CardDatabase
from fcsim.cli import main

SCENARIOS = load_scenarios()
OK = {"verdict": "ok", "findings": [], "hooks": []}


def finding(kind: str, summary: str, basis: str = "rule 302.6") -> dict:
    return {"kind": kind, "summary": summary, "basis": basis, "fix": "rewind", "confidence": "high"}


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    path = tmp_path_factory.mktemp("cards") / "cards.sqlite"
    fixture = Path(__file__).parent / "data" / "cards.jsonl"
    cards = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
    rulings = [{"oracle_id": c["oracle_id"], "published_at": "2020-01-01", "comment": "A ruling about Sol Ring."}
               for c in cards if c["name"] == "Sol Ring"]
    CardDatabase.build(cards, path, source="fixture", updated_at="", rulings=rulings)
    return CardDatabase(path)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["id"])
def test_every_scenario_sets_up_and_plays_as_written(scenario):
    table, runner = build(scenario)
    before = len(table.events)
    play(scenario, runner)
    assert len(table.events) == before + len(scenario["script"])
    assert scenario["about"] and ("findings" in scenario["expect"])


def test_the_suite_has_both_tiers_and_clean_plays_to_catch_false_alarms():
    assert {s["tier"] for s in SCENARIOS} == {"ordinary", "weird"} and len({s["id"] for s in SCENARIOS}) == len(SCENARIOS)
    clean = [s for s in SCENARIOS if not s["expect"]["findings"]]
    assert len(clean) >= 8 and len(SCENARIOS) - len(clean) >= 16


def test_materials_show_the_board_the_record_with_steps_and_the_cards(database):
    scenario = next(s for s in SCENARIOS if s["id"] == "sol-ring-pays-two")
    table, runner = build(scenario, database)
    auditor = Auditor(FakeRunner([OK]), database)
    snapshot = auditor.snapshot(table)
    play(scenario, runner)
    text = materials(table, snapshot[0], snapshot[2], database, snapshot[1])
    assert "THE TABLE BEFORE" in text and "Sol Ring [c" in text and "1 cards in hand" in text
    assert "(Ann, cast, during main1): Ann casts Hill Giant" in text
    assert "table warning: Hill Giant has mana value 4 and 3 permanent(s) were tapped" not in text  # not in the fixture
    assert "{T}: Add {C}{C}." in text and "ruling 2020-01-01: A ruling about Sol Ring." in text
    assert "Wastes" not in text  # nothing hidden: libraries and hands are counts
    verdict, reply = auditor.audit(table, snapshot)
    assert verdict == OK and auditor.runner.asked[0]["schema"] is VERDICT_SCHEMA


def test_scoring_counts_misses_false_alarms_and_hooks():
    planted = next(s for s in SCENARIOS if s["id"] == "summoning-sick-attack")
    clean = next(s for s in SCENARIOS if s["id"] == "haste-attack-is-legal")
    hooked = next(s for s in SCENARIOS if s["id"] == "hook-does-not-untap")
    caught = {"findings": [finding("illegal", "Grizzly Bears has summoning sickness and no haste.")], "hooks": []}
    assert score(planted, caught)["passed"]
    assert score(planted, {"findings": [], "hooks": []})["missed"] == ["haste|summoning|continuously|302"]
    assert score(planted, {"findings": [finding("note", "maybe summoning sick?")], "hooks": []})["missed"]  # a note is no catch
    wrong = score(clean, caught)
    assert not wrong["passed"] and wrong["false_alarms"] == ["Grizzly Bears has summoning sickness and no haste."]
    assert score(clean, {"findings": [finding("note", "unsure about haste")], "hooks": []})["passed"]  # notes never block
    assert score(hooked, {"findings": [], "hooks": []})["hooks_missed"] == ["untap"]
    assert score(hooked, {"findings": [], "hooks": [{"kind": "hold", "source": "Mana Vault", "step": "untap",
                                                     "text": "does not untap", "seat": 0}]})["passed"]


def test_a_suite_run_reports_rates_and_stops_at_the_budget(database):
    chosen = [s for s in SCENARIOS if s["id"] in ("summoning-sick-attack", "haste-attack-is-legal", "second-land")]
    answers = [{"verdict": "findings", "findings": [finding("illegal", "no haste")], "hooks": []},
               {"verdict": "findings", "findings": [finding("illegal", "no haste")], "hooks": []}, OK]
    report = run_suite(Auditor(FakeRunner(answers), database), chosen)
    summary = report["summary"]
    assert summary["scenarios"] == 3 and summary["passed"] == 1
    assert summary["catch_rate"] == 0.5 and summary["clean_plays_flagged"] == "1 of 1"
    stopped = run_suite(Auditor(FakeRunner([OK, OK, OK], Budget(max_calls=2)), database), chosen)
    assert "skipped" in stopped["results"][-1] and stopped["summary"]["scenarios"] == 2
    with pytest.raises(BudgetExceeded):
        Budget(max_cost_usd=1.0, cost_usd=1.0).check()


def test_the_audit_command_lists_shows_and_runs(database, tmp_path, capsys, monkeypatch):
    (tmp_path / "scryfall").mkdir()
    (tmp_path / "scryfall" / "cards.sqlite").write_bytes(database.path.read_bytes())
    monkeypatch.setenv("FCSIM_DATA", str(tmp_path))
    assert main(["audit", "list"]) == 0 and "fizzle-still-gains-life" in capsys.readouterr().out
    assert main(["audit", "show", "second-land"]) == 0
    shown = capsys.readouterr().out
    assert "WHAT WAS DONE" in shown and "table warning: This is land number 2 this turn." in shown and "EXPECTED" in shown
    assert main(["audit", "show", "nonsense"]) == 2
    capsys.readouterr()
    monkeypatch.setattr("fcsim.cli.ClaudeCodeRunner", lambda **options: FakeRunner(
        [{"verdict": "findings", "findings": [finding("illegal", "A second land was played.")], "hooks": []}, OK],
        Budget(max_calls=options["budget"].max_calls)))
    out = tmp_path / "report.json"
    assert main(["audit", "suite", "--only", "second-land", "second-land-with-exploration", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "pass  ordinary second-land " in printed and "2 of 2 scenarios passed" in printed
    assert json.loads(out.read_text())["summary"]["catch_rate"] == 1.0
