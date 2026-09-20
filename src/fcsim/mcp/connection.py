"""The tool access granted to one model call, independent of its CLI backend."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class McpConnection:
    name: str
    url: str
    headers: dict[str, str] = field(repr=False)
    allowed_tools: tuple[str, ...] = ()
