"""Qt user interface for flacblind."""

from __future__ import annotations

__all__ = ["MainWindow", "Player", "Translator", "tr"]


def __getattr__(name: str):  # pragma: no cover - lazy re-exports
    """Import GUI symbols lazily so ``import flacblind.ui`` stays cheap."""
    if name == "MainWindow":
        from .main_window import MainWindow

        return MainWindow
    if name == "Player":
        from .player import Player

        return Player
    if name in ("Translator", "tr"):
        from . import i18n

        return getattr(i18n, name)
    raise AttributeError(name)
