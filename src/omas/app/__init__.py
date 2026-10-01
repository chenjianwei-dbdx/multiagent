"""Composition root (ADR D19: offline AutoBinder until a model is configured)."""

from .auto_binder import AutoBinder
from .bootstrap import AppContainer, build_app

__all__ = ["AppContainer", "AutoBinder", "build_app"]
