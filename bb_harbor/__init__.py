"""Browserbase-backed Harbor integrations."""

from typing import TYPE_CHECKING

TRAJECTORIES_ROOT = "/logs/agent/trajectories"
TRAJECTORY_POINTER_PATH = "/logs/agent/stagehand-trajectory-dir"

__all__ = [
    "BrowserbaseDockerEnvironment",
    "BrowserbaseEnvironment",
    "StagehandAgent",
    "StagehandVerifier",
    "TRAJECTORIES_ROOT",
    "TRAJECTORY_POINTER_PATH",
]

if TYPE_CHECKING:
    from .agent import StagehandAgent
    from .env import BrowserbaseDockerEnvironment, BrowserbaseEnvironment
    from .verifier import StagehandVerifier


def __getattr__(name: str):
    if name in {"BrowserbaseDockerEnvironment", "BrowserbaseEnvironment"}:
        from .env import BrowserbaseDockerEnvironment, BrowserbaseEnvironment

        value = (
            BrowserbaseDockerEnvironment
            if name == "BrowserbaseDockerEnvironment"
            else BrowserbaseEnvironment
        )
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
