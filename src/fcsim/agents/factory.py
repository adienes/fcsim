"""Backend selection lives here, not in the game or audit drivers."""

from .codex import CodexRunner
from .runner import DEFAULT_MODEL, ClaudeCodeRunner, Runner, RunnerError

BACKENDS = ("claude", "codex")


def make_runner(backend: str = "claude", *, model: str | None = None, **options) -> Runner:
    if backend == "claude":
        return ClaudeCodeRunner(model=model or DEFAULT_MODEL, **options)
    if backend == "codex":
        return CodexRunner(model=model, **options)
    raise RunnerError(f"Unknown model backend {backend!r}; choose from {', '.join(BACKENDS)}.")
