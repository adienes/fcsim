"""Checks on the source itself, for mistakes no behaviour test happened to cover."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "fcsim"


def test_no_statement_sits_where_it_can_never_run():
    """A line left behind a `raise` (or a return, continue or break) in its own block never runs. One such
    line was the announcement of every block in combat: for a whole batch of games each block was logged
    as "no blocks", and the auditor had correct combat "corrected"."""
    unreachable = []
    for path in sorted(SOURCE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for statement, following in zip(block, block[1:]):
                    if isinstance(statement, (ast.Raise, ast.Return, ast.Continue, ast.Break)):
                        unreachable.append(f"{path.relative_to(SOURCE)}:{following.lineno}")
    assert unreachable == []
