"""Fresh Codex CLI sessions using the local login and only the supplied MCP tools."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..mcp.connection import McpConnection
from .runner import Budget, Reply, RunnerError


# Keep the coding assistant's capabilities out of the game. Read-only sandboxing
# alone is insufficient: a shell could read opponents' hands from the game files.
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "view_image", "apps", "plugins", "remote_plugin",
    "multi_agent", "multi_agent_v2", "browser_use", "browser_use_external", "computer_use",
    "image_generation", "hooks", "goals", "memories", "skill_search", "skill_mcp_dependency_install",
    "workspace_dependencies", "code_mode", "code_mode_host", "sleep_tool", "tool_suggest",
    "in_app_browser", "in_app_local_automation", "shell_snapshot", "default_mode_request_user_input",
)


def strict_schema(schema: dict) -> dict:
    """Represent optional properties as required/nullable for strict structured output.

    The original schema remains the public contract. strip_optional_nulls reverses
    this representation before callers see the response.
    """
    result = dict(schema)
    if "properties" in schema:
        required = schema.get("required", [])
        result["properties"] = {
            key: strict_schema(value) if key in required else {"anyOf": [strict_schema(value), {"type": "null"}]}
            for key, value in schema["properties"].items()
        }
        result["required"] = list(schema["properties"])
        result["additionalProperties"] = False
    if "items" in schema:
        result["items"] = strict_schema(schema["items"])
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in schema:
            result[keyword] = [strict_schema(value) for value in schema[keyword]]
    return result


def strip_optional_nulls(value, schema: dict):
    if isinstance(value, dict) and "properties" in schema:
        properties, required = schema["properties"], schema.get("required", [])
        return {key: strip_optional_nulls(item, properties.get(key, {})) for key, item in value.items()
                if not (key in properties and key not in required and item is None)}
    if isinstance(value, list):
        return [strip_optional_nulls(item, schema.get("items", {})) for item in value]
    return value


class CodexRunner:
    backend = "codex"

    def __init__(self, *, model: str | None = None, effort: str | None = None, budget: Budget | None = None,
                 timeout: int = 600, executable: str = "codex"):
        self.model = model or "codex-default"
        self._model = model
        self.effort, self.timeout, self.executable = effort, timeout, executable
        self.budget = budget or Budget(max_cost_usd=None)
        if self.budget.max_cost_usd is not None:
            raise RunnerError("Codex reports tokens but no USD estimate; use --max-cost none and --max-calls to bound the run.")
        if effort not in (None, "minimal", "low", "medium", "high", "xhigh"):
            raise RunnerError(f"Unsupported Codex effort: {effort!r}; use minimal, low, medium, high or xhigh.")
        self._checked = False

    def _check_cli(self) -> None:
        if self._checked:
            return
        if shutil.which(self.executable) is None:
            raise RunnerError(f"`{self.executable}` is not on PATH. Install Codex CLI and log in.")
        try:
            help_result = subprocess.run([self.executable, "exec", "--help"], capture_output=True, text=True,
                                         timeout=15, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RunnerError("Codex CLI did not respond to `exec --help`.") from exc
        required = ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--output-schema", "--output-last-message")
        if help_result.returncode or any(flag not in help_result.stdout for flag in required):
            raise RunnerError("Update Codex CLI: fcsim needs exec with --ignore-user-config, --ignore-rules, "
                              "--ephemeral and structured output (tested with 0.155.1).")
        self._checked = True

    def _command(self, directory: Path, system: str, schema: dict | None,
                 tools: McpConnection | None) -> list[str]:
        instructions = directory / "instructions.md"
        instructions.write_text(system, encoding="utf-8")
        command = [self.executable, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                   "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never", "--json",
                   "--output-last-message", str(directory / "answer.txt")]
        options = {
            "approval_policy": "never", "web_search": "disabled", "project_doc_max_bytes": 0,
            "history.persistence": "none", "model_instructions_file": str(instructions),
            "features.skip_host_skill_discovery": True,
            **{f"features.{name}": False for name in DISABLED_FEATURES},
        }
        if self._model:
            command += ["--model", self._model]
        if self.effort:
            options["model_reasoning_effort"] = self.effort
        if schema is not None:
            schema_path = directory / "schema.json"
            schema_path.write_text(json.dumps(strict_schema(schema)), encoding="utf-8")
            command += ["--output-schema", str(schema_path)]
        if tools is not None:
            # CLI override paths split on dots without TOML quoted-key parsing.
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", tools.name):
                raise RunnerError("Codex MCP server names must use letters, digits, underscores or hyphens.")
            prefix = f"mcp_servers.{tools.name}"
            options.update({f"{prefix}.url": tools.url, f"{prefix}.required": True,
                            f"{prefix}.enabled_tools": list(tools.allowed_tools),
                            f"{prefix}.default_tools_approval_mode": "approve"})
            headers = "{" + ", ".join(f"{json.dumps(key)} = {json.dumps(value)}" for key, value in tools.headers.items()) + "}"
            command += ["--config", f"{prefix}.http_headers={headers}"]
        for key, value in options.items():
            command += ["--config", f"{key}={json.dumps(value, ensure_ascii=False)}"]
        return command + ["-"]

    def ask(self, system: str, prompt: str, *, schema: dict | None = None,
            tools: McpConnection | None = None) -> Reply:
        self._check_cli()
        with tempfile.TemporaryDirectory(prefix="fcsim-") as scratch:
            directory = Path(scratch)
            command = self._command(directory, system, schema, tools)
            self.budget.begin()
            started = time.monotonic()
            try:
                done = subprocess.run(command, input=prompt, capture_output=True, text=True, cwd=scratch,
                                      timeout=self.timeout, check=False)
            except (subprocess.TimeoutExpired, OSError) as exc:
                self.budget.record(Reply(None, "", seconds=time.monotonic() - started, model=self.model))
                raise RunnerError(f"Codex failed to finish (timeout {self.timeout}s): {type(exc).__name__}") from exc
            answer_file = directory / "answer.txt"
            answer = answer_file.read_text(encoding="utf-8") if answer_file.exists() else ""

        reply = Reply(None, answer, seconds=time.monotonic() - started, model=self.model)
        error, completed = None, False
        for line in done.stdout.splitlines():
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event is not an object")
            except ValueError:
                error = "Codex returned malformed JSONL."
                break
            if event.get("type") == "turn.completed":
                completed = True
                usage = event.get("usage") or {}
                # Claude reports uncached input separately; normalize Codex's
                # inclusive count so shared budgets can sum the two backends.
                counts = {key: value for key, value in usage.items() if isinstance(value, int)}
                cached = counts.get("cached_input_tokens", 0)
                written = counts.get("cache_write_input_tokens", 0)
                normalized = {"input_tokens": max(0, counts.get("input_tokens", 0) - cached - written),
                              "cache_read_input_tokens": cached, "cache_creation_input_tokens": written,
                              "output_tokens": counts.get("output_tokens", 0),
                              "reasoning_output_tokens": counts.get("reasoning_output_tokens", 0)}
                for key, value in normalized.items():
                    reply.tokens[key] = reply.tokens.get(key, 0) + value
            elif event.get("type") in ("turn.failed", "error"):
                error = "Codex reported an error: " + str(event.get("error") or event.get("message") or event)[:500]
        self.budget.record(reply)
        if done.returncode or error or not completed or not answer.strip():
            detail = error or (done.stderr[:500] if done.returncode else "No completed turn and final answer.")
            if tools:
                for secret in tools.headers.values():
                    detail = detail.replace(secret, "[redacted]")
            raise RunnerError(f"Codex did not finish successfully (exit {done.returncode}): {detail}")
        if schema is not None:
            try:
                data = json.loads(answer)
            except ValueError as exc:
                raise RunnerError("Codex did not answer with the requested JSON object.") from exc
            if not isinstance(data, dict):
                raise RunnerError("Codex did not answer with the requested JSON object.")
            reply.data = strip_optional_nulls(data, schema)
        return reply
