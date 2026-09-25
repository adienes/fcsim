from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from fcsim.agents import Budget, BudgetExceeded, ClaudeCodeRunner, CodexRunner, Reply, RunnerError, cost_summary, make_runner
from fcsim.agents.codex import strict_schema, strip_optional_nulls
from fcsim.audit.auditor import VERDICT_SCHEMA
from fcsim.cli import main
from fcsim.mcp import McpConnection


CONNECTION = McpConnection("fcsim", "http://127.0.0.1:12345/mcp", {"Authorization": "Bearer test"}, ("get_view", "keep"))
HELP = "--ignore-user-config --ignore-rules --ephemeral --output-schema --output-last-message"


def test_factory_defaults_are_specific_to_backend():
    assert isinstance(make_runner(), ClaudeCodeRunner)
    assert make_runner().model == "claude-opus-5-5"
    assert make_runner("codex").model == "codex-default"
    assert make_runner("codex", model="chosen").model == "chosen"
    with pytest.raises(RunnerError, match="Unknown model backend"):
        make_runner("unknown")
    with pytest.raises(RunnerError, match="no USD estimate"):
        make_runner("codex", budget=Budget())
    with pytest.raises(RunnerError, match="Unsupported Codex effort"):
        make_runner("codex", effort="max")


def test_unknown_cost_is_not_zero_and_call_limit_still_applies():
    budget = Budget(max_calls=2, max_cost_usd=None)
    budget.begin()
    budget.record(Reply(None, "claude", cost_usd=.4, tokens={"input_tokens": 10}))
    budget.begin()
    budget.record(Reply(None, "codex", tokens={"input_tokens": 20}))
    assert budget.to_dict()["estimated_cost_usd"] is None
    assert budget.to_dict()["reported_cost_usd"] == .4
    assert budget.tokens["input_tokens"] == 30
    assert "unavailable" in cost_summary(budget.to_dict())
    with pytest.raises(BudgetExceeded):
        budget.begin()


@pytest.mark.parametrize("limit", [None, 5])
def test_claude_receives_only_the_granted_mcp_tools(monkeypatch, limit):
    monkeypatch.setattr("shutil.which", lambda _: "/bin/claude")
    def run(command, **kwargs):
        assert kwargs["input"] == "wake"
        assert command[command.index("--system-prompt") + 1] == "system"
        assert command[command.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in command and "--no-session-persistence" in command
        config = json.loads(command[command.index("--mcp-config") + 1])
        assert config["mcpServers"]["fcsim"]["headers"] == CONNECTION.headers
        assert command[command.index("--allowedTools") + 1] == "mcp__fcsim__get_view,mcp__fcsim__keep"
        assert ("--max-budget-usd" in command) == (limit is not None)
        return subprocess.CompletedProcess(command, 0, json.dumps({"result": "kept", "total_cost_usd": .2}), "")
    monkeypatch.setattr(subprocess, "run", run)
    runner = ClaudeCodeRunner(budget=Budget(max_cost_usd=limit))
    assert runner.ask("system", "wake", tools=CONNECTION).text == "kept"
    assert runner.budget.cost_usd == .2


def codex_stub(monkeypatch, *, events=None, answer='{"ok":true}', returncode=0, failure=None):
    calls = []
    monkeypatch.setattr("shutil.which", lambda _: "/bin/codex")
    def run(command, **kwargs):
        if command == ["codex", "exec", "--help"]:
            return subprocess.CompletedProcess(command, 0, HELP, "")
        calls.append((command, kwargs))
        if failure:
            raise failure
        Path(command[command.index("--output-last-message") + 1]).write_text(answer)
        lines = events if events is not None else [{"type": "turn.completed", "usage": {
            "input_tokens": 30, "cached_input_tokens": 20, "output_tokens": 5}}]
        return subprocess.CompletedProcess(command, returncode, "\n".join(json.dumps(e) for e in lines), "failure")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_codex_configuration_and_accounting(monkeypatch):
    calls = codex_stub(monkeypatch)
    runner = CodexRunner(effort="high")
    reply = runner.ask("system", "wake", schema={"type": "object", "properties": {"ok": {"type": "boolean"}}}, tools=CONNECTION)
    command, options = calls[0]
    assert options["input"] == "wake"
    assert not Path(options["cwd"]).exists()  # private instructions and answer were removed
    assert "--model" not in command  # never send Claude's default model to Codex
    config = tomllib.loads("\n".join(command[i + 1] for i, value in enumerate(command) if value == "--config"))
    assert config["approval_policy"] == "never" and config["web_search"] == "disabled"
    assert config["features"]["shell_tool"] is False and config["features"]["plugins"] is False
    assert config["features"]["skip_host_skill_discovery"] is True
    assert config["mcp_servers"]["fcsim"]["enabled_tools"] == ["get_view", "keep"]
    assert config["mcp_servers"]["fcsim"]["http_headers"] == CONNECTION.headers
    assert reply.data == {"ok": True} and reply.cost_usd is None
    assert runner.budget.tokens["input_tokens"] == 10
    assert runner.budget.tokens["cache_read_input_tokens"] == 20
    assert runner.budget.to_dict()["estimated_cost_usd"] is None


@pytest.mark.parametrize("events,answer,returncode", [
    ([{"type": "turn.failed", "error": {"message": "failed"}}], "answer", 0),
    ([{"type": "turn.completed"}], "answer", 1),
    ([], "answer", 0),
    ([{"type": "turn.completed"}], "", 0),
    (["not an event"], "answer", 0),
])
def test_codex_failed_attempts_are_counted(monkeypatch, events, answer, returncode):
    codex_stub(monkeypatch, events=events, answer=answer, returncode=returncode)
    runner = CodexRunner(budget=Budget(max_calls=1, max_cost_usd=None))
    with pytest.raises(RunnerError):
        runner.ask("system", "wake")
    assert runner.budget.calls == 1 and runner.budget.unpriced_calls == 1
    with pytest.raises(BudgetExceeded):
        runner.ask("system", "wake")


def test_codex_timeout_and_invalid_structured_output(monkeypatch):
    codex_stub(monkeypatch, failure=subprocess.TimeoutExpired("codex", 1))
    runner = CodexRunner(timeout=1)
    with pytest.raises(RunnerError, match="timeout"):
        runner.ask("system", "wake")
    assert runner.budget.calls == runner.budget.unpriced_calls == 1
    codex_stub(monkeypatch, answer="not json")
    with pytest.raises(RunnerError, match="JSON object"):
        runner.ask("system", "wake", schema=VERDICT_SCHEMA)
    assert runner.budget.calls == 2


def test_old_codex_fails_before_a_model_attempt(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "old help", ""))
    runner = CodexRunner()
    with pytest.raises(RunnerError, match="Update Codex CLI"):
        runner.ask("system", "wake")
    assert runner.budget.calls == 0


def test_optional_schema_fields_round_trip_without_null_hooks():
    strict = strict_schema(VERDICT_SCHEMA)
    hook_schema = strict["properties"]["hooks"]["items"]
    assert set(hook_schema["required"]) == set(hook_schema["properties"])
    assert "seat" not in VERDICT_SCHEMA["properties"]["hooks"]["items"]["required"]
    hook = {"kind": "event_trigger", "handle": "c1", "source": "Example", "text": "Trigger",
            "step": None, "seat": None, "whose": None, "controller": None}
    answer = {"verdict": "ok", "findings": [], "hooks": [hook]}
    restored = strip_optional_nulls(answer, VERDICT_SCHEMA)
    assert restored["hooks"][0] == {key: value for key, value in hook.items() if value is not None}


@pytest.mark.parametrize("command", ["goldfish", "pod"])
@pytest.mark.parametrize("backend,auditor,cost", [("claude", None, 15), ("codex", None, None), ("claude", "codex", None)])
def test_cli_selects_backends_and_shared_budget(monkeypatch, tmp_path, command, backend, auditor, cost):
    from fcsim import cli
    (tmp_path / "report.md").write_text("Report")
    seen = []
    def play(*args, **kwargs):
        seen.extend(args[1] if command == "pod" else [args[1]])
        seen.append(args[2])
        return {"directory": str(tmp_path), "stopped": False}
    monkeypatch.setattr(cli, "goldfish" if command == "goldfish" else "play_pod", play)
    monkeypatch.setattr(cli, "CardDatabase", lambda: None)
    monkeypatch.setattr(cli, "RulesReference", lambda: None)
    argv = [command, "deck.txt"] + (["other.txt"] if command == "pod" else []) + ["--backend", backend]
    if auditor:
        argv += ["--audit-backend", auditor]
    assert main(argv) == 0
    assert all(r.backend == backend for r in seen[:-1])
    assert seen[-1].backend == (auditor or backend)
    assert all(r.budget is seen[0].budget for r in seen)
    assert seen[0].budget.max_cost_usd == (40 if command == "pod" and cost else cost)


def test_cli_rejects_an_unenforceable_dollar_limit(capsys):
    assert main(["goldfish", "deck.txt", "--backend", "codex", "--max-cost", "1"]) == 2
    assert "Codex has no USD estimate" in capsys.readouterr().err


def test_the_session_limits_own_message_says_how_long_to_wait():
    import datetime

    from fcsim.agents import seconds_until_reset
    said = "model error: Claude Code reported an error: You've hit your session limit · resets 3:30pm (America/New_York)"
    assert seconds_until_reset(said, datetime.datetime(2026, 9, 21, 13, 35)) == 115 * 60 + 120
    assert seconds_until_reset(said, datetime.datetime(2026, 9, 21, 15, 31)) == (24 * 60 - 1) * 60 + 120      # that time has passed: tomorrow's
    assert seconds_until_reset("limit · resets 12am", datetime.datetime(2026, 9, 21, 23, 0)) is None             # not the session limit's message
    assert seconds_until_reset("You've hit your session limit · resets 12am", datetime.datetime(2026, 9, 21, 23, 0)) == 3600 + 120
    assert seconds_until_reset("model error: The model did not answer within 600 seconds.") is None


def test_an_error_on_the_apis_own_side_is_tried_again_and_the_rest_are_not(monkeypatch):
    import json
    import subprocess

    from fcsim.agents import runner
    from fcsim.agents.runner import ClaudeCodeRunner, RunnerError, transient

    assert transient("Claude Code reported an error: API Error: 529 Overloaded. This is a server-side issue, usually temporary")
    assert transient("API Error: 500 Internal server error") and not transient("You've hit your session limit · resets 3:30pm") \
        and not transient("The model did not answer in the requested form: hello")
    answers = [{"is_error": True, "result": "API Error: 500 Internal server error. This is a server-side issue, usually temporary — try again in a moment."},
               {"is_error": True, "result": "API Error: 529 Overloaded"},
               {"result": "fine", "structured_output": {"ok": True}, "total_cost_usd": 0.01, "usage": {}}]
    slept, told = [], []
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(runner.subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0, stdout=json.dumps(answers.pop(0)), stderr=""))
    monkeypatch.setattr(runner.time, "sleep", slept.append)
    monkeypatch.setattr(ClaudeCodeRunner, "tell", staticmethod(told.append))
    reply = ClaudeCodeRunner().ask("system", "prompt", schema={"type": "object"})
    assert reply.data == {"ok": True} and slept == [30, 90] and len(told) == 2 and "529 Overloaded" in told[1]
    answers[:] = [{"is_error": True, "result": "You've hit your session limit · resets 3:30pm (America/New_York)"}]
    with pytest.raises(RunnerError, match="session limit"):
        ClaudeCodeRunner().ask("system", "prompt")
    assert slept == [30, 90]                                                                     # not the API's trouble: raised at once
    answers[:] = [{"is_error": True, "result": "API Error: 529 Overloaded"}] * 4
    with pytest.raises(RunnerError, match="529"):
        ClaudeCodeRunner().ask("system", "prompt")
    assert slept == [30, 90, 30, 90, 180]                                                        # three more tries, then it is raised
    assert transient("The model did not answer within 600 seconds.")                            # a hung call is tried again too
    from fcsim.mcp.connection import McpConnection
    answers[:] = [{"is_error": True, "result": "API Error: 529 Overloaded"}]
    with pytest.raises(RunnerError, match="529"):                                               # a session with tools is its caller's to retry
        ClaudeCodeRunner().ask("system", "prompt", tools=McpConnection(name="table", url="http://127.0.0.1:1/mcp", headers={}, allowed_tools=("pass_priority",)))
    assert slept == [30, 90, 30, 90, 180]
