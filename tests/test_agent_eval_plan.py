from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path

import pytest

from bb_harbor.agent import StagehandAgent, StagehandAgentFailedError
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext

from conftest import RecordingBaseEnvironment


TASK_ID = "agent/iframe_form"
INSTRUCTION = f"stagehand-task-id: {TASK_ID}"


def test_eval_command_uses_positional_target_and_single_trial(
    agent_factory: Callable[..., StagehandAgent],
) -> None:
    agent = agent_factory(
        mode="dom",
        model_name="google/gemini-3-flash-preview",
    )

    command = agent._build_eval_command(TASK_ID, use_browserbase=True)
    preview_command = agent._build_eval_command(
        TASK_ID,
        use_browserbase=True,
        preview=True,
    )

    assert command == (
        "evals run agent/iframe_form --trials 1 --concurrency 1 "
        "--agent-mode dom --env browserbase "
        "--model google/gemini-3-flash-preview"
    )
    assert preview_command == f"{command} --preview"
    argv = shlex.split(command)
    assert argv[2] == TASK_ID
    assert argv[argv.index("--trials") + 1] == "1"
    assert "--task" not in argv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "preview_result",
    [
        ExecResult(stderr="preview process failed", return_code=2),
        ExecResult(
            stdout=(
                '  error: No runnable tasks found matching "agent/iframe_form".\n'
            ),
            return_code=0,
        ),
        ExecResult(
            stdout="  Target: agent/iframe_form  →  no tasks (0 tasks)\n",
            return_code=0,
        ),
    ],
    ids=("nonzero-return-code", "rendered-error-branch", "zero-task-header"),
)
async def test_setup_rejects_failed_explicit_task_preview(
    tmp_path: Path,
    agent_factory: Callable[..., StagehandAgent],
    preview_result: ExecResult,
) -> None:
    def script(command: str, env: dict[str, str]) -> ExecResult:
        del env
        if command == "evals --help":
            return ExecResult(stdout="usage", return_code=0)
        if command.startswith("evals run") and "--preview" in shlex.split(command):
            return preview_result
        raise AssertionError(f"unexpected command: {command!r}")

    environment = RecordingBaseEnvironment(tmp_path, script)

    with pytest.raises(StagehandAgentFailedError):
        await agent_factory(task_id=TASK_ID).setup(environment)


@pytest.mark.asyncio
async def test_run_stops_before_billable_eval_when_preview_does_not_resolve(
    tmp_path: Path,
    agent_factory: Callable[..., StagehandAgent],
) -> None:
    def script(command: str, env: dict[str, str]) -> ExecResult:
        del env
        if command.startswith("evals run") and "--preview" in shlex.split(command):
            return ExecResult(
                stdout=(
                    '  error: No runnable tasks found matching "agent/iframe_form".\n'
                ),
                return_code=0,
            )
        return ExecResult(return_code=0)

    environment = RecordingBaseEnvironment(tmp_path, script)

    with pytest.raises(StagehandAgentFailedError):
        await agent_factory().run(INSTRUCTION, environment, AgentContext())

    eval_commands = [
        call["command"]
        for call in environment.calls
        if call["command"].startswith("evals run")
    ]
    assert eval_commands
    assert all("--preview" in shlex.split(command) for command in eval_commands)
