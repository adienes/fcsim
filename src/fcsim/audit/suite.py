"""The rules regression suite: written scenarios with known answers, to measure an auditor.

A scenario sets up a board, plays a short script, and says what a correct audit finds: either
nothing (a legal play that may look wrong) or particular findings (a planted error). Scores
count misses and false alarms alike, since a false alarm stalls a game as surely as a miss
corrupts one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..agents import BudgetExceeded, RunnerError, cost_summary
from ..table import STEPS, ScriptedRunner, Table
from ..table.model import Control, Obj
from .auditor import SERIOUS, Auditor

SUITE_DIR = Path(__file__).with_name("suite")


def load_scenarios(directory: Path = SUITE_DIR) -> list[dict]:
    scenarios = []
    for path in sorted(directory.glob("*.json")):
        for scenario in json.loads(path.read_text(encoding="utf-8")):
            scenarios.append({"tier": path.stem, **scenario})
    return scenarios


def _place(table: Table, seat: int, zone: str, spec: str | dict) -> list[Obj]:
    game = table.game
    spec = {"card": spec} if isinstance(spec, str) else dict(spec)
    made = []
    for _ in range(spec.get("count", 1)):
        obj = Obj(id=game.new_id(), uid=0, owner=spec.get("owner", seat), controller=seat, zone=zone,
                  name=spec.get("card") or spec["token"]["name"], token=spec.get("token"),
                  is_commander=bool(spec.get("commander")), tapped=bool(spec.get("tapped")),
                  counters=dict(spec.get("counters", {})), damage=spec.get("damage", 0),
                  annotations=[{"text": n, "expires": "eot"} if isinstance(n, str) else n for n in spec.get("notes", [])],
                  entered_turn=game.turn.number if spec.get("new") else game.turn.number - 1,
                  public=zone != "hand", known_by={seat} if zone == "hand" else set())
        obj.uid = obj.id
        game.objects[obj.id] = obj
        (game.battlefield if zone == "battlefield" else getattr(game.seats[obj.owner], zone)).append(obj.id)
        made.append(obj)
    return made


def build(scenario: dict, database=None) -> tuple[Table, ScriptedRunner]:
    """Set the board up directly (this part is not logged), ready for the script to be played."""
    names = scenario.get("seats", ["Ann", "Bob"])
    commanders = scenario.get("commanders", {})
    active = scenario.get("active", 0)
    table = Table([{"name": name, "commanders": commanders.get(name, []), "library": ["Wastes"] * 40}
                   for name in names], seed=1, first=active,
                  card_lookup=(lambda n: (c := database.find(n)) and c.table_facts()) if database else None)
    for seat in range(len(names)):
        table.act(seat, "keep")
    game = table.game
    for seat in game.seats:  # the dealt hands go back; the scenario says what is in hand
        for obj_id in list(seat.hand):
            seat.hand.remove(obj_id)
            seat.library.append(obj_id)
            game.objects[obj_id].zone, game.objects[obj_id].known_by = "library", set()
        seat.life = scenario.get("life", {}).get(seat.name, 40)
        seat.commander_casts = dict(scenario.get("commander_casts", {}).get(seat.name, {}))
    step = scenario.get("step", "main1")
    game.turn.number, game.turn.active, game.turn.step = scenario.get("turn", 4), active, step
    game.turn.remaining = list(STEPS[STEPS.index(step) + 1:])
    game.turn.passed, game.turn.land_plays = [], scenario.get("land_plays", 0)
    game.control = Control("priority", [scenario.get("priority", active)])
    placed: dict[str, Obj] = {}
    attach: list[tuple[Obj, str]] = []
    for zone in ("battlefield", "hand", "graveyard", "exile"):
        for name, specs in scenario.get(zone, {}).items():
            for spec in specs:
                for obj in _place(table, names.index(name), zone, spec):
                    placed[obj.name] = obj
                    if isinstance(spec, dict) and spec.get("attached_to"):
                        attach.append((obj, spec["attached_to"]))
    for seat_name in scenario.get("commanders_in_play", []):  # a commander that starts on the battlefield
        seat = game.seats[names.index(seat_name)]
        for obj_id in list(seat.command):
            seat.command.remove(obj_id)
            obj = game.objects[obj_id]
            obj.zone, obj.entered_turn = "battlefield", game.turn.number - 1
            game.battlefield.append(obj_id)
    for obj, host in attach:
        obj.attached_to = placed[host].id
    game.ledger += scenario.get("ledger", [])
    return table, ScriptedRunner(table)


def play(scenario: dict, runner: ScriptedRunner) -> None:
    for step in scenario["script"]:
        seat, tool, *rest = step
        runner.step(seat, tool, rest[0] if rest else None)


def _matches(finding: dict, expected: dict) -> bool:
    text = " ".join(str(finding.get(key, "")) for key in ("summary", "basis", "fix"))
    return finding.get("kind") in expected.get("kinds", SERIOUS) and \
        re.search(expected["mentions"], text, flags=re.IGNORECASE) is not None


def score(scenario: dict, verdict: dict) -> dict:
    """Compare a verdict with the scenario's expectation."""
    expect = scenario["expect"]
    serious = [f for f in verdict.get("findings", []) if f.get("kind") in SERIOUS]
    wanted = expect.get("findings", [])
    missed = [e["mentions"] for e in wanted if not any(_matches(f, e) for f in serious)]
    extra = [f for f in serious if not any(_matches(f, e) for e in wanted)
             and not any(re.search(p, f.get("summary", ""), flags=re.IGNORECASE) for p in expect.get("tolerate", []))]
    hooks, wanted_hooks = verdict.get("hooks", []), expect.get("hooks", [])

    def hook_matches(hook: dict, expected: dict) -> bool:
        """The kind, the words, and every behavioural field the scenario pins down (step, seat, whose)."""
        return hook.get("kind") == expected["kind"] and re.search(expected["mentions"], json.dumps(hook), flags=re.IGNORECASE) \
            and all(hook.get(key) == expected[key] for key in ("step", "seat", "whose") if key in expected)

    hook_misses = [e["mentions"] for e in wanted_hooks if not any(hook_matches(h, e) for h in hooks)]
    # A scenario lists every hook its new permanents need, so any other hook is a false alarm:
    # a bogus "hold" would hand a step to a player for no reason.
    hook_extras = [f"{h.get('kind')} {h.get('step', '')} {h.get('source')}: {h.get('text', '')}".replace("  ", " ")
                   for h in hooks if h.get("kind") != "static" and not any(hook_matches(h, e) for e in wanted_hooks)]
    return {"passed": not missed and not extra and not hook_misses and not hook_extras, "expected_findings": len(wanted),
            "missed": missed, "false_alarms": [f["summary"] for f in extra], "hooks_expected": len(wanted_hooks),
            "hooks_missed": hook_misses, "hooks_unexpected": hook_extras}


def run_suite(auditor: Auditor, scenarios: list[dict], *, progress=None) -> dict:
    results = []
    for scenario in scenarios:
        table, runner = build(scenario, auditor.database)
        snapshot = auditor.snapshot(table)
        play(scenario, runner)
        try:
            verdict, reply = auditor.audit(table, snapshot)
        except BudgetExceeded as stop:
            results.append({"id": scenario["id"], "tier": scenario["tier"], "skipped": str(stop)})
            break
        except RunnerError as error:
            results.append({"id": scenario["id"], "tier": scenario["tier"], "error": str(error), "passed": False})
            continue
        result = {"id": scenario["id"], "tier": scenario["tier"], "about": scenario["about"], **score(scenario, verdict),
                  "seconds": round(reply.seconds, 1), "estimated_cost_usd": reply.estimated_cost_usd, "verdict": verdict}
        results.append(result)
        if progress:
            progress(result)
    done = [r for r in results if "passed" in r and "error" not in r]
    expected = sum(r["expected_findings"] for r in done)
    clean = [r for r in done if not r["expected_findings"]]
    hooks = sum(r["hooks_expected"] for r in done)
    summary = {
        "model": auditor.runner.model, "backend": getattr(auditor.runner, "backend", "custom"),
        "scenarios": len(done), "passed": sum(r["passed"] for r in done),
        "catch_rate": round(1 - sum(len(r["missed"]) for r in done) / expected, 3) if expected else None,
        "false_alarm_scenarios": sum(bool(r["false_alarms"]) for r in done),
        "clean_plays_flagged": f"{sum(bool(r['false_alarms']) for r in clean)} of {len(clean)}",
        "hook_accuracy": round(1 - sum(len(r["hooks_missed"]) for r in done) / hooks, 3) if hooks else None,
        "unexpected_hooks": sum(len(r.get("hooks_unexpected", [])) for r in done),
        "seconds_per_audit": round(sum(r["seconds"] for r in done) / len(done), 1) if done else None,
        "budget": auditor.runner.budget.to_dict(),
    }
    return {"summary": summary, "results": results}


def render(report: dict) -> str:
    lines = []
    for r in report["results"]:
        if "skipped" in r:
            lines.append(f"  stop  {r['id']}: {r['skipped']}")
            continue
        if "error" in r:
            lines.append(f"  ERR   {r['id']}: {r['error'][:200]}")
            continue
        lines.append(f"  {'pass' if r['passed'] else 'FAIL'}  {r['tier']:<8} {r['id']:<34} {r['seconds']:>5.1f}s  {r['about']}")
        lines += [f"          missed: {m}" for m in r["missed"]]
        lines += [f"          false alarm: {f}" for f in r["false_alarms"]]
        lines += [f"          hook missed: {h}" for h in r["hooks_missed"]]
        lines += [f"          unexpected hook: {h}" for h in r.get("hooks_unexpected", [])]
    s = report["summary"]
    if s is None:
        return "\n".join(lines)
    lines += ["", f"{s['model']}: {s['passed']} of {s['scenarios']} scenarios passed. "
                  f"Catch rate {s['catch_rate']}, false alarms in {s['false_alarm_scenarios']} scenario(s) "
                  f"(clean plays flagged: {s['clean_plays_flagged']}), hook accuracy {s['hook_accuracy']}, "
                  f"{s['unexpected_hooks']} unexpected hook(s).",
              f"{s['seconds_per_audit']}s per audit; {s['budget']['calls']} calls, "
              f"{cost_summary(s['budget'])}."]
    return "\n".join(lines)
