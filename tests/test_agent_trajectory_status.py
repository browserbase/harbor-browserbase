from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from pathlib import Path

import pytest

from bb_harbor import TRAJECTORIES_ROOT, TRAJECTORY_POINTER_PATH
from bb_harbor.agent import (
    StagehandAgent,
    StagehandAgentFailedError,
    StagehandRolloutFailedError,
)
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext

from conftest import RecordingBaseEnvironment


INSTRUCTION = "stagehand-task-id: agent/columbia_tuition"


def _script_trajectory(
    trajectory_dir: str,
    cat_result: ExecResult,
    *,
    eval_stdout: str | None = None,
    eval_stderr: str | None = None,
    preview_stdout: str | None = None,
    preview_stderr: str | None = None,
    preview_return_code: int = 0,
) -> Callable[[str, dict[str, str]], ExecResult]:
    trajectory_path = f"{trajectory_dir}/trajectory.json"
    cat_command = f"cat {shlex.quote(trajectory_path)}"

    def script(command: str, env: dict[str, str]) -> ExecResult:
        del env
        if command.startswith("evals run") and "--preview" in shlex.split(command):
            return ExecResult(
                stdout=(
                    "  Target: agent/columbia_tuition  →  core (1 task)\n"
                    if preview_stdout is None
                    else preview_stdout
                ),
                stderr=preview_stderr,
                return_code=preview_return_code,
            )
        if command.startswith("evals run"):
            return ExecResult(
                stdout=eval_stdout,
                stderr=eval_stderr,
                return_code=0,
            )
        if command.startswith("find "):
            return ExecResult(
                stdout=f"1\t42\t{trajectory_path}\n",
                return_code=0,
            )
        if command == cat_command:
            return cat_result
        return ExecResult(return_code=0)

    return script


@pytest.mark.asyncio
async def test_agent_raises_with_trajectory_and_eval_diagnostics(
    tmp_path: Path,
    agent_factory: Callable[..., StagehandAgent],
) -> None:
    trajectory_dir = (
        f"{TRAJECTORIES_ROOT}/trial/group/agent/columbia_tuition/error-run"
    )
    payload = {
        "task": {"id": "agent/columbia_tuition"},
        "steps": [{"action": "start"}],
        "status": " ERROR ",
        "usage": {"input_tokens": 17, "output_tokens": 9},
        "error": {
            "name": "RolloutError",
            "message": "trajectory-error-marker",
        },
    }
    environment = RecordingBaseEnvironment(
        tmp_path,
        _script_trajectory(
            trajectory_dir,
            ExecResult(stdout=json.dumps(payload), return_code=0),
            eval_stderr="ExperimentalNotConfiguredError: Agent callbacks",
        ),
    )
    agent = agent_factory(mode="hybrid")
    context = AgentContext()

    with pytest.raises(StagehandRolloutFailedError) as excinfo:
        await agent.run(INSTRUCTION, environment, context)

    message = str(excinfo.value)
    assert "ExperimentalNotConfiguredError" in message
    assert "trajectory-error-marker" in message
    assert "step_count=1" in message
    assert "usage.input_tokens=17" in message
    assert "usage.output_tokens=9" in message
    assert "status='ERROR'" in message
    assert "task_id='agent/columbia_tuition'" in message
    assert "mode='hybrid'" in message
    assert f"trajectory_path='{trajectory_dir}/trajectory.json'" in message
    assert context.metadata["stagehand"]["trajectory_dir"] == trajectory_dir
    assert not any(
        TRAJECTORY_POINTER_PATH in call["command"] for call in environment.calls
    )


@pytest.mark.asyncio
async def test_healthy_trajectory_writes_pointer(
    tmp_path: Path,
    agent_factory: Callable[..., StagehandAgent],
) -> None:
    trajectory_dir = (
        f"{TRAJECTORIES_ROOT}/trial/group/agent/columbia_tuition/success-run"
    )
    payload = {
        "task": {"id": "agent/columbia_tuition"},
        "steps": [{"action": "navigate"}],
        "status": "success",
        "usage": {"input_tokens": 100, "output_tokens": 25},
    }
    environment = RecordingBaseEnvironment(
        tmp_path,
        _script_trajectory(
            trajectory_dir,
            ExecResult(stdout=json.dumps(payload), return_code=0),
        ),
    )

    await agent_factory().run(INSTRUCTION, environment, AgentContext())

    assert any(
        TRAJECTORY_POINTER_PATH in call["command"] for call in environment.calls
    )


@pytest.mark.asyncio
async def test_zero_score_success_is_left_to_verifier(
    tmp_path: Path,
    agent_factory: Callable[..., StagehandAgent],
) -> None:
    trajectory_dir = (
        f"{TRAJECTORIES_ROOT}/trial/group/agent/columbia_tuition/zero-score-run"
    )
    payload = {
        "task": {"id": "agent/columbia_tuition"},
        # Work evidence is deliberately non-zero: this test isolates scoring,
        # which remains the verifier's concern, from agent rollout health.
        "steps": [{"action": "inspect"}],
        "status": "success",
        "usage": {"input_tokens": 11, "output_tokens": 2},
        "scores": {"reward": 0.0, "criteria_earned_frac": 0.0},
        "outcomeSuccess": False,
        "criteria_earned_frac": 0.0,
        "extractions": [],
    }
    environment = RecordingBaseEnvironment(
        tmp_path,
        _script_trajectory(
            trajectory_dir,
            ExecResult(stdout=json.dumps(payload), return_code=0),
        ),
    )

    await agent_factory().run(INSTRUCTION, environment, AgentContext())

    assert any(
        TRAJECTORY_POINTER_PATH in call["command"] for call in environment.calls
    )


@pytest.mark.asyncio
async def test_complete_zero_work_trajectory_raises_without_pointer(
    tmp_path: Path,
    agent_factory: Callable[..., StagehandAgent],
) -> None:
    trajectory_dir = (
        f"{TRAJECTORIES_ROOT}/trial/group/agent/columbia_tuition/zero-work-run"
    )
    payload = {
        "task": {"id": "agent/columbia_tuition"},
        "steps": [],
        "status": "complete",
        "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0},
    }
    environment = RecordingBaseEnvironment(
        tmp_path,
        _script_trajectory(
            trajectory_dir,
            ExecResult(stdout=json.dumps(payload), return_code=0),
            eval_stdout="eval-output-marker",
            eval_stderr="eval-error-marker",
        ),
    )

    with pytest.raises(StagehandRolloutFailedError) as excinfo:
        await agent_factory().run(INSTRUCTION, environment, AgentContext())

    message = str(excinfo.value)
    assert "task_id='agent/columbia_tuition'" in message
    assert "did no work" in message
    assert "usage.cached_tokens=0" in message
    assert "eval-output-marker" in message
    assert "eval-error-marker" in message
    assert not any(
        TRAJECTORY_POINTER_PATH in call["command"] for call in environment.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("eval_stdout", "eval_stderr"),
    [
        ('Error: Unknown option "--task"\n', None),
        (None, '  Error: Unknown option "--task"\n'),
        ('\x1b[31mError: Unknown option "--task"\x1b[39m\n', None),
    ],
    ids=("stdout", "stderr", "ansi-colored"),
)
async def test_zero_exit_cli_failure_signature_raises(
    tmp_path: Path,
    agent_factory: Callable[..., StagehandAgent],
    eval_stdout: str | None,
    eval_stderr: str | None,
) -> None:
    trajectory_dir = (
        f"{TRAJECTORIES_ROOT}/trial/group/agent/columbia_tuition/unused-run"
    )
    environment = RecordingBaseEnvironment(
        tmp_path,
        _script_trajectory(
            trajectory_dir,
            ExecResult(stdout="{}", return_code=0),
            eval_stdout=eval_stdout,
            eval_stderr=eval_stderr,
        ),
    )

    with pytest.raises(StagehandAgentFailedError) as excinfo:
        await agent_factory().run(INSTRUCTION, environment, AgentContext())

    message = str(excinfo.value)
    assert 'Unknown option "--task"' in message
    assert "return_code=0" in message
    assert "eval stdout:" in message
    assert "eval stderr:" in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cat_result",
    [
        ExecResult(stderr="cat failed", return_code=1),
        ExecResult(stdout="{not valid json", return_code=0),
    ],
    ids=("unreadable", "invalid-json"),
)
async def test_unreadable_trajectory_status_is_lenient(
    tmp_path: Path,
    agent_factory: Callable[..., StagehandAgent],
    cat_result: ExecResult,
) -> None:
    trajectory_dir = (
        f"{TRAJECTORIES_ROOT}/trial/group/agent/columbia_tuition/unreadable-run"
    )
    environment = RecordingBaseEnvironment(
        tmp_path,
        _script_trajectory(trajectory_dir, cat_result),
    )

    await agent_factory().run(INSTRUCTION, environment, AgentContext())

    assert any(
        TRAJECTORY_POINTER_PATH in call["command"] for call in environment.calls
    )
