"""A minimal Model Context Protocol server over HTTP, standard library only."""

from .http import McpServer
from .connection import McpConnection

__all__ = ["McpConnection", "McpServer"]
