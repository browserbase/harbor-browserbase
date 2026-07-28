"""Browserbase-backed Harbor integrations."""

from typing import TYPE_CHECKING

__all__ = [
    "BrowserbaseEnvironment",
    "StagehandAgent",
    "StagehandVerifier",
]

if TYPE_CHECKING:
    from .agent import StagehandAgent
    from .env import BrowserbaseEnvironment
    from .verifier import StagehandVerifier


def __getattr__(name: str):
    if name == "BrowserbaseEnvironment":
        from .env import BrowserbaseEnvironment

        value = BrowserbaseEnvironment
    elif name == "StagehandAgent":
        from .agent import StagehandAgent

        value = StagehandAgent
    elif name == "StagehandVerifier":
        from .verifier import StagehandVerifier

        value = StagehandVerifier
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value
