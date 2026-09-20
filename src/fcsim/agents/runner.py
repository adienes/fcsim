"""One model call at a time, through the Claude Code command line in headless mode.

This uses whatever account the local `claude` is logged in with, so a subscription works and
no API key is needed. Each call is a fresh session with no built-in tools, no user or project
settings and no other MCP servers: the model sees the system prompt and the prompt, nothing else.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field


class RunnerError(RuntimeError):
    """The model could not be run, or did not answer in the form asked for."""


class BudgetExceeded(RunnerError):
    """A hard limit was reached. Nothing further is sent."""


@dataclass
class Budget:
    """Hard limits for a run, checked before every call. `cost_usd` is Claude Code's estimate at
    list prices; on a subscription nothing is billed, but it tracks how much of the plan is used."""
    max_calls: int = 50
    max_cost_usd: float = 5.0
    calls: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)

    def check(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"The run's limit of {self.max_calls} model calls is used up.")
        if self.cost_usd >= self.max_cost_usd:
            raise BudgetExceeded(f"The run's limit of ${self.max_cost_usd:.2f} (estimated) is used up.")

    def record(self, reply: Reply) -> None:
        self.calls += 1
        self.cost_usd += reply.cost_usd
        self.seconds += reply.seconds
        for key, value in reply.tokens.items():
            self.tokens[key] = self.tokens.get(key, 0) + value

    def to_dict(self) -> dict:
        return {"calls": self.calls, "max_calls": self.max_calls, "estimated_cost_usd": round(self.cost_usd, 4),
                "max_cost_usd": self.max_cost_usd, "seconds": round(self.seconds, 1), "tokens": self.tokens}


@dataclass
class Reply:
    data: dict | None          # the structured answer, when a schema was given
    text: str
    cost_usd: float = 0.0
    seconds: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)
    model: str = ""


class ClaudeCodeRunner:
    def __init__(self, *, model: str = "claude-opus-5", effort: str | None = None, budget: Budget | None = None,
                 timeout: int = 600, executable: str = "claude"):
        self.model, self.effort, self.timeout, self.executable = model, effort, timeout, executable
        self.budget = budget or Budget()

    def ask(self, system: str, prompt: str, *, schema: dict | None = None, mcp_config: dict | None = None,
            allowed_tools: tuple[str, ...] = ()) -> Reply:
        """One fresh session. With `schema`, the reply's `data` is validated JSON of that shape."""
        self.budget.check()
        if shutil.which(self.executable) is None:
            raise RunnerError(f"`{self.executable}` is not on PATH. Install Claude Code and log in.")
        with tempfile.TemporaryDirectory(prefix="fcsim-") as scratch:  # an empty directory: no CLAUDE.md to find
            command = [self.executable, "--print", "--output-format", "json", "--model", self.model,
                       "--system-prompt", system, "--tools", "", "--strict-mcp-config",
                       "--setting-sources", "", "--no-session-persistence",
                       "--max-budget-usd", f"{max(self.budget.max_cost_usd - self.budget.cost_usd, 0.01):.2f}"]
            if self.effort:
                command += ["--effort", self.effort]
            if schema is not None:
                command += ["--json-schema", json.dumps(schema)]
            if mcp_config is not None:
                command += ["--mcp-config", json.dumps(mcp_config)]
            if allowed_tools:
                command += ["--allowedTools", ",".join(allowed_tools)]
            started = time.monotonic()
            try:
                done = subprocess.run(command, input=prompt, capture_output=True, text=True, cwd=scratch,
                                      timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise RunnerError(f"The model did not answer within {self.timeout} seconds.") from exc
            seconds = time.monotonic() - started
        try:
            result = json.loads(done.stdout)
        except ValueError as exc:
            raise RunnerError(f"Claude Code did not return JSON (exit {done.returncode}): "
                              f"{(done.stderr or done.stdout)[:500]}") from exc
        usage = result.get("usage") or {}
        reply = Reply(data=result.get("structured_output"), text=str(result.get("result", "")),
                      cost_usd=float(result.get("total_cost_usd") or 0), seconds=seconds, model=self.model,
                      tokens={key: int(usage.get(key) or 0) for key in
                              ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")})
        self.budget.record(reply)
        if result.get("is_error"):
            raise RunnerError(f"Claude Code reported an error: {reply.text[:500]}")
        if schema is not None and not isinstance(reply.data, dict):
            raise RunnerError(f"The model did not answer in the requested form: {reply.text[:500]}")
        return reply


class FakeRunner:
    """Answers from a list, for tests. Records what it was asked."""

    def __init__(self, answers: list[dict], budget: Budget | None = None):
        self.answers, self.asked, self.budget = list(answers), [], budget or Budget()
        self.model = "fake"

    def ask(self, system: str, prompt: str, *, schema: dict | None = None, **_) -> Reply:
        self.budget.check()
        self.asked.append({"system": system, "prompt": prompt, "schema": schema})
        reply = Reply(data=self.answers.pop(0), text="", cost_usd=0.01, seconds=0.0, model="fake")
        self.budget.record(reply)
        return reply
