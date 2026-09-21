"""Runner contract, shared accounting, and the Claude Code and test adapters."""

from __future__ import annotations

import datetime
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Protocol

from ..mcp.connection import McpConnection


def seconds_until_reset(message: str, now: datetime.datetime | None = None) -> int | None:
    """How long until the subscription's session limit resets, from what Claude Code says when it is reached ("You've hit your
    session limit · resets 3:30pm (America/New_York)"), or None if the message is about something else. The time is read as
    this machine's local time, with two minutes to spare."""
    found = re.search(r"session limit.*?resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", message, re.I | re.S)
    if not found:
        return None
    now = now or datetime.datetime.now()
    hour = int(found[1]) % 12 + (12 if found[3].lower() == "pm" else 0)
    reset = now.replace(hour=hour, minute=int(found[2] or 0), second=0, microsecond=0)
    if reset <= now:
        reset += datetime.timedelta(days=1)
    return int((reset - now).total_seconds()) + 120


class RunnerError(RuntimeError):
    """The model could not be run, or did not answer in the form asked for."""


class BudgetExceeded(RunnerError):
    """A hard limit was reached. Nothing further is sent."""


@dataclass
class Budget:
    """Attempt limit and optional reported-cost limit, shared by every role in a run.

    Backends without dollar estimates must use max_cost_usd=None. Their usage is
    counted in calls and tokens; a missing cost is never reported as zero.
    """
    max_calls: int = 50
    max_cost_usd: float | None = 5.0
    calls: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)
    unpriced_calls: int = 0

    def check(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"The run's limit of {self.max_calls} model calls is used up.")
        if self.max_cost_usd is not None and self.cost_usd >= self.max_cost_usd:
            raise BudgetExceeded(f"The run's limit of ${self.max_cost_usd:.2f} (estimated) is used up.")

    def begin(self) -> None:
        """Count the attempt before it is launched, so a call that fails still uses up the limit."""
        self.check()
        self.calls += 1

    def record(self, reply: Reply) -> None:
        if reply.cost_usd is None:
            self.unpriced_calls += 1
        else:
            self.cost_usd += reply.cost_usd
        self.seconds += reply.seconds
        for key, value in reply.tokens.items():
            self.tokens[key] = self.tokens.get(key, 0) + value

    def to_dict(self) -> dict:
        return {"calls": self.calls, "max_calls": self.max_calls,
                "estimated_cost_usd": None if self.unpriced_calls else round(self.cost_usd, 4),
                "reported_cost_usd": round(self.cost_usd, 4), "unpriced_calls": self.unpriced_calls,
                "max_cost_usd": self.max_cost_usd, "seconds": round(self.seconds, 1), "tokens": self.tokens}


@dataclass
class Reply:
    data: dict | None          # the structured answer, when a schema was given
    text: str
    cost_usd: float | None = None
    seconds: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)
    model: str = ""

    @property
    def estimated_cost_usd(self) -> float | None:
        return round(self.cost_usd, 4) if self.cost_usd is not None else None


class Runner(Protocol):
    backend: str
    model: str
    budget: Budget

    def ask(self, system: str, prompt: str, *, schema: dict | None = None,
            tools: McpConnection | None = None) -> Reply: ...


def cost_summary(usage: dict) -> str:
    cost = usage["estimated_cost_usd"]
    if cost is None:
        return (f"USD estimate unavailable for {usage['unpriced_calls']} call(s); "
                f"${usage['reported_cost_usd']:.2f} reported for the remaining calls")
    return f"${cost:.2f} estimated at list prices (subscription usage, not a charge)"


class ClaudeCodeRunner:
    backend = "claude"

    def __init__(self, *, model: str = "claude-opus-5", effort: str | None = None, budget: Budget | None = None,
                 timeout: int = 600, executable: str = "claude"):
        if effort not in (None, "low", "medium", "high", "xhigh", "max"):
            raise RunnerError(f"Unsupported Claude effort: {effort!r}; use low, medium, high, xhigh or max.")
        self.model, self.effort, self.timeout, self.executable = model, effort, timeout, executable
        self.budget = budget or Budget()

    def ask(self, system: str, prompt: str, *, schema: dict | None = None,
            tools: McpConnection | None = None) -> Reply:
        """One fresh session, requesting structured output when a schema is supplied."""
        if shutil.which(self.executable) is None:
            raise RunnerError(f"`{self.executable}` is not on PATH. Install Claude Code and log in.")
        self.budget.begin()
        with tempfile.TemporaryDirectory(prefix="fcsim-") as scratch:  # an empty directory: no CLAUDE.md to find
            command = [self.executable, "--print", "--output-format", "json", "--model", self.model,
                       "--system-prompt", system, "--tools", "", "--strict-mcp-config",
                       "--setting-sources", "", "--no-session-persistence"]
            if self.budget.max_cost_usd is not None:
                command += ["--max-budget-usd", f"{max(self.budget.max_cost_usd - self.budget.cost_usd, 0.01):.2f}"]
            if self.effort:
                command += ["--effort", self.effort]
            if schema is not None:
                command += ["--json-schema", json.dumps(schema)]
            if tools is not None:
                config = {"mcpServers": {tools.name: {"type": "http", "url": tools.url, "headers": tools.headers}}}
                command += ["--mcp-config", json.dumps(config)]
                command += ["--allowedTools", ",".join(f"mcp__{tools.name}__{name}" for name in tools.allowed_tools)]
            started = time.monotonic()
            try:
                done = subprocess.run(command, input=prompt, capture_output=True, text=True, cwd=scratch,
                                      timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                self.budget.seconds += time.monotonic() - started  # its cost is unknown; its time and the attempt count
                raise RunnerError(f"The model did not answer within {self.timeout} seconds.") from exc
            seconds = time.monotonic() - started
        try:
            result = json.loads(done.stdout)
        except ValueError as exc:
            self.budget.seconds += seconds
            raise RunnerError(f"Claude Code did not return JSON (exit {done.returncode}): "
                              f"{(done.stderr or done.stdout)[:500]}") from exc
        usage = result.get("usage") or {}
        reply = Reply(data=result.get("structured_output"), text=str(result.get("result", "")),
                      cost_usd=float(result.get("total_cost_usd") or 0), seconds=seconds, model=self.model,
                      tokens={key: int(usage.get(key) or 0) for key in
                              ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")})
        self.budget.record(reply)
        if done.returncode or result.get("is_error"):
            raise RunnerError(f"Claude Code reported an error: {reply.text[:500]}")
        if schema is not None and not isinstance(reply.data, dict):
            raise RunnerError(f"The model did not answer in the requested form: {reply.text[:500]}")
        return reply


class FakeRunner:
    """Answers from a list, for tests. Records what it was asked."""

    backend = "fake"

    def __init__(self, answers: list[dict], budget: Budget | None = None):
        self.answers, self.asked, self.budget = list(answers), [], budget or Budget()
        self.model = "fake"

    def ask(self, system: str, prompt: str, *, schema: dict | None = None, **_) -> Reply:
        self.budget.begin()
        self.asked.append({"system": system, "prompt": prompt, "schema": schema})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        reply = Reply(data=answer, text="", cost_usd=0.01, seconds=0.0, model="fake")
        self.budget.record(reply)
        return reply
