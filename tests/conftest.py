from __future__ import annotations

import inspect
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bb_harbor.agent import StagehandAgent
from bb_harbor.env import BrowserbaseEnvironment
from bb_harbor.verifier import StagehandVerifier
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths


class RecordingBaseEnvironment(BaseEnvironment):
    """Minimal concrete environment retaining Harbor's real scoped env merge."""

    def __init__(self, tmp_path: Path, script=None, *, session_id: str = "trial__env"):
        self.calls: list[dict[str, Any]] = []
        self.script = script
        super().__init__(
            environment_dir=tmp_path,
            environment_name="recording",
            session_id=session_id,
            trial_paths=TrialPaths(tmp_path / "trial"),
            task_env_config=EnvironmentConfig(),
        )

    @staticmethod
    def type() -> str:
        return "recording"

    def _validate_definition(self) -> None:
        return None

    async def start(self, force_build: bool) -> None:
        return None

    async def stop(self, delete: bool) -> None:
        return None

    async def upload_file(self, source_path, target_path):
        return None

    async def upload_dir(self, source_dir, target_dir):
        return None

    async def download_file(self, source_path, target_path):
        return None

    async def download_dir(self, source_dir, target_dir):
        return None

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        merged_env = self._merge_env(env) or {}
        self.calls.append(
            {
                "command": command,
                "cwd": cwd,
                "env": merged_env,
                "timeout_sec": timeout_sec,
                "user": user,
            }
        )
        if self.script is None:
            return ExecResult(return_code=0)
        result = self.script(command, merged_env)
        if inspect.isawaitable(result):
            result = await result
        return result


class RecordingBrowserbaseEnvironment(BrowserbaseEnvironment):
    """Browserbase environment double that keeps the real session scope and merge."""

    def __init__(self, *args, script=None, **kwargs):
        self.calls: list[dict[str, Any]] = []
        self.script = script
        super().__init__(*args, **kwargs)

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        merged_env = self._merge_env(env) or {}
        self.calls.append(
            {
                "command": command,
                "cwd": cwd,
                "env": merged_env,
                "timeout_sec": timeout_sec,
                "user": user,
            }
        )
        if self.script is None:
            return ExecResult(return_code=0)
        result = self.script(command, merged_env)
        if inspect.isawaitable(result):
            result = await result
        return result


class FakeSessions:
    def __init__(self, session_id: str = "bb-session", connect_url: str = "wss://bb"):
        self.session_id = session_id
        self.connect_url = connect_url
        self.create_calls: list[dict[str, Any]] = []
        self.retrieve_calls: list[str] = []
        self.update_calls: list[tuple[str, str]] = []
        self.update_started = None
        self.update_gate = None
        self.update_completed = False

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return SimpleNamespace(id=self.session_id, connect_url=self.connect_url)

    async def retrieve(self, session_id: str):
        self.retrieve_calls.append(session_id)
        return SimpleNamespace(id=session_id)

    async def update(self, session_id: str, *, status: str):
        self.update_calls.append((session_id, status))
        if self.update_started is not None:
            self.update_started.set()
        if self.update_gate is not None:
            await self.update_gate.wait()
        self.update_completed = True
        return SimpleNamespace(id=session_id)


class FakeAsyncBrowserbase:
    instances: list["FakeAsyncBrowserbase"] = []
    sessions_factory = FakeSessions

    def __init__(self, *, api_key: str):
        self.api_key = api_key
        self.sessions = self.sessions_factory()
        self.instances.append(self)


@pytest.fixture
def browserbase_env_factory(tmp_path):
    def factory(
        *,
        session_id: str = "trial__env",
        script=None,
    ) -> RecordingBrowserbaseEnvironment:
        environment_dir = tmp_path / f"environment-{session_id}"
        environment_dir.mkdir()
        (environment_dir / "Dockerfile").write_text("FROM scratch\n")
        trial_paths = TrialPaths(tmp_path / f"trial-{session_id}")
        return RecordingBrowserbaseEnvironment(
            environment_dir=environment_dir,
            environment_name=f"env-{session_id}",
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=EnvironmentConfig(),
            script=script,
        )

    return factory


@pytest.fixture
def agent_factory(tmp_path):
    def factory(**kwargs) -> StagehandAgent:
        defaults = {
            "logs_dir": tmp_path / "agent-logs",
            "model_name": "test/model",
            "extra_env": {
                "BROWSERBASE_API_KEY": "test-key",
                "BROWSERBASE_PROJECT_ID": "test-project",
            },
        }
        defaults.update(kwargs)
        return StagehandAgent(**defaults)

    return factory


@pytest.fixture
def verifier_factory(tmp_path):
    def factory(environment: BaseEnvironment, **kwargs) -> StagehandVerifier:
        # BaseVerifier only stores Task and TrialPaths; a stand-in avoids loading an
        # unrelated task fixture in tests focused purely on result translation.
        defaults = {
            "task": SimpleNamespace(name="test-task"),
            "trial_paths": TrialPaths(tmp_path / "verifier-trial"),
            "environment": environment,
            "logger": logging.getLogger("tests.stagehand-verifier"),
            "trajectory_dir": "/trajectory/run",
        }
        defaults.update(kwargs)
        return StagehandVerifier(**defaults)

    return factory
