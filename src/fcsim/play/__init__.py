"""Drivers that run games: agents at a table, an auditor behind them, a directory of what happened."""

from .goldfish import goldfish
from .pod import play_pod

__all__ = ["goldfish", "play_pod"]
