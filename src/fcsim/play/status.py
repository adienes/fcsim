"""Read a game directory: where the game stands, and where its time went.

Works on a game that is still being played, since the journal, transcripts and audits are
appended as it goes.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .gamedir import GameDirectory


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line] if path.is_file() else []


def status(game_dir: str | Path) -> dict:
    directory = GameDirectory.open(game_dir)
    config, events = directory.read("config.json"), directory.events()
    names = config.get("seats") or [seat["name"] for seat in config["table"]["seats"]]
    state = directory.read("state.json") if (directory.path / "state.json").is_file() else None
    rows = [row for index in range(len(names)) for row in _lines(directory.path / f"transcript-seat-{index}.jsonl")]
    wakes = [r for r in rows if r.get("kind") != "triage" and "seconds" in r]
    triage = [r for r in rows if r.get("kind") == "triage"]
    audits = _lines(directory.path / "audits.jsonl")
    last = events[-1]
    by_turn: dict[int, dict] = {}
    for wake in wakes:
        turn = by_turn.setdefault(wake.get("table_turn", wake["turn"]), {"wakes": 0, "seconds": 0.0, "tool_calls": 0})
        turn["wakes"] += 1
        turn["seconds"] += wake["seconds"]
        turn["tool_calls"] += wake["tool_calls"]
    for audit in audits:
        by_turn.setdefault(audit["turn"], {"wakes": 0, "seconds": 0.0, "tool_calls": 0})["audit_seconds"] = \
            by_turn[audit["turn"]].get("audit_seconds", 0) + (audit.get("seconds") or 0)
    idle = [w for w in wakes if not any(c["tool"] not in ("get_view", "get_notes", "get_log", "lookup_card", "lookup_rule",
                                                           "pass_priority", "write_notes") for c in w.get("calls", []))]
    return {
        "directory": str(directory.path), "seats": names, "events": len(events),
        "at": {"table_turn": last["turn"], "step": last["step"]},
        "standings": [{"name": s["name"], "status": s["status"], "life": s["life"], "hand": len(s["hand"]),
                       "permanents": sum(1 for o in state["objects"].values() if o["zone"] == "battlefield" and o["controller"] == s["index"]),
                       "commander_casts": s["commander_casts"]} for s in state["seats"]] if state else [],
        "winner": names[state["winner"]] if state and state.get("winner") is not None else None,
        "time": {
            "wakes": len(wakes), "wake_seconds": round(sum(w["seconds"] for w in wakes)),
            "seconds_per_wake": round(sum(w["seconds"] for w in wakes) / len(wakes), 1) if wakes else None,
            "wakes_that_only_looked_and_passed": len(idle), "their_seconds": round(sum(w["seconds"] for w in idle)),
            "tool_calls": sum(w["tool_calls"] for w in wakes),
            "calls_by_tool": dict(Counter(c["tool"] for w in wakes for c in w.get("calls", [])).most_common(10)),
            "rejected_calls": dict(Counter(e["error"] for w in wakes for e in w["tool_errors"])),
            "wakes_the_driver_finished": sum(1 for w in wakes if w.get("forced") or w.get("forced_to_end")),
            "triage_questions": len(triage), "triage_said_respond": sum(1 for t in triage if t["respond"]),
            "audits": len(audits), "audit_seconds": round(sum(a.get("seconds") or 0 for a in audits)),
            "slowest_wakes": [{"seat": names[w["seat"]] if "seat" in w else names[0], "turn": w.get("table_turn", w["turn"]),
                               "kind": w["kind"], "seconds": w["seconds"], "tool_calls": w["tool_calls"]}
                              for w in sorted(wakes, key=lambda w: -w["seconds"])[:5]],
        },
        "by_turn": {turn: {k: round(v, 1) if isinstance(v, float) else v for k, v in row.items()} for turn, row in sorted(by_turn.items())},
        "findings": [{"turn": a["turn"], "kind": f["kind"], "seat": f.get("seat"), "summary": f["summary"]}
                     for a in audits for f in a.get("findings", []) if f["kind"] != "note"],
    }


def render(report: dict) -> str:
    at, time = report["at"], report["time"]
    lines = [f"{report['directory']}", f"{report['events']} events; at table turn {at['table_turn']}, {at['step']}"
             + (f"; winner: {report['winner']}" if report["winner"] else ""), ""]
    lines += [f"  {s['name']:<18}{s['status']:<11}{s['life']:>4} life  {s['hand']} in hand  {s['permanents']:>2} permanents"
              + (f"  commander cast x{sum(s['commander_casts'].values())}" if s["commander_casts"] else "") for s in report["standings"]]
    lines += ["", f"TIME  {time['wakes']} wakes, {time['wake_seconds']}s of model time ({time['seconds_per_wake']}s each), "
                  f"{time['tool_calls']} tool calls; {time['audits']} audits, {time['audit_seconds']}s; "
                  f"{time['triage_questions']} respond-or-pass questions ({time['triage_said_respond']} said respond)",
              f"  {time['wakes_that_only_looked_and_passed']} wakes only looked and passed ({time['their_seconds']}s); "
              f"{time['wakes_the_driver_finished']} the driver had to finish; rejected calls {time['rejected_calls'] or 'none'}",
              "  calls: " + ", ".join(f"{tool} {n}" for tool, n in time["calls_by_tool"].items()),
              "  slowest: " + "; ".join(f"{w['seat']} turn {w['turn']} {w['kind']} {w['seconds']:.0f}s/{w['tool_calls']} calls" for w in time["slowest_wakes"]),
              "", "BY TABLE TURN  wakes, model seconds, tool calls, audit seconds"]
    lines += [f"  {turn:>3}  {row['wakes']:>3} {row['seconds']:>7.0f}s {row['tool_calls']:>4} {row.get('audit_seconds', 0):>6.0f}s" for turn, row in report["by_turn"].items()]
    if report["findings"]:
        lines += ["", "AUDITOR FINDINGS"] + [f"  turn {f['turn']} {f['kind']} (seat {f['seat']}): {f['summary'][:150]}" for f in report["findings"]]
    return "\n".join(lines)
