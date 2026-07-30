from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from pathlib import Path

import pytest

from bb_harbor.agent import StagehandAgent, StagehandAgentFailedError
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext

from conftest import RecordingBaseEnvironment


TASK_ID = "agent/iframe_form"
SESSION_ID = "6f43ca91-8038-45a4-8c67-178529f8cf55"
SECOND_SESSION_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SESSION_URL = f"https://www.browserbase.com/sessions/{SESSION_ID}"
REAL_CAPTURED_LINE = (
    "2026-07-30T00:13:56.976Z::[stagehand:init] Browserbase session started "
    '{"sessionUrl":{"value":"'
    + SESSION_URL
    + '","type":"string"},"debugUrl":{"value":"https://www.browserbase.com/'
    "devtools/inspector.html?wss=connect.browserbase.com/debug/"
    + SESSION_ID
    + '/devtools/page/37D241183949D10DA9B647810F06945A?debug=true","type":"…'
)


def test_extracts_session_id_from_real_ansi_pty_line() -> None:
    captured = f"\x1b[32m{REAL_CAPTURED_LINE}\x1b[0m\r\n"

    assert StagehandAgent._extract_browserbase_session_ids(captured) == [SESSION_ID]


def test_clipped_session_url_does_not_produce_partial_id() -> None:
    clipped = (
        "2026-07-30T00:13:56.976Z::[stagehand:init] Browserbase session started "
        '...{"sessionUrl":{"value":"https://www.browserbase…'
    )

    assert StagehandAgent._extract_browserbase_session_ids(clipped) == []


def test_multiple_ids_publish_in_order_and_select_last(
    agent_factory: Callable[..., StagehandAgent],
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = agent_factory(mode="hybrid")
    context = AgentContext()
    captured = (
        f"{SESSION_URL}\r\n{SESSION_URL}\r\n"
        f"https://www.browserbase.com/sessions/{SECOND_SESSION_ID}\r\n"
    )

    agent._publish_browserbase_session(
        context=context,
        task_id=TASK_ID,
        result=ExecResult(stdout=captured, return_code=0),
    )

    artifact = json.loads(
        (Path(agent.logs_dir) / "browserbase_session.json").read_text()
    )
    assert artifact["all_session_ids"] == [SESSION_ID, SECOND_SESSION_ID]
    assert artifact["session_id"] == SECOND_SESSION_ID
    assert context.metadata["stagehand"]["browserbase_session_ids"] == [
        SESSION_ID,
        SECOND_SESSION_ID,
    ]
    assert "Captured 2 distinct Browserbase session ids" in caplog.text


def test_artifact_has_exact_safe_shape(
    agent_factory: Callable[..., StagehandAgent],
) -> None:
    agent = agent_factory(mode="dom")

    agent._publish_browserbase_session(
        context=AgentContext(),
        task_id=TASK_ID,
        result=ExecResult(stdout=REAL_CAPTURED_LINE, return_code=0),
    )

    artifact_path = Path(agent.logs_dir) / "browserbase_session.json"
    raw_artifact = artifact_path.read_text()
    artifact = json.loads(raw_artifact)
    assert raw_artifact.endswith("\n")
    assert artifact == {
        "session_id": SESSION_ID,
        "session_url": SESSION_URL,
        "debug_url": None,
        "task_id": TASK_ID,
        "mode": "dom",
        "all_session_ids": [SESSION_ID],
    }
    assert set(artifact) == {
        "session_id",
        "session_url",
        "debug_url",
        "task_id",
        "mode",
        "all_session_ids",
    }
    assert "wss" not in raw_artifact
    assert "connect.browserbase.com" not in raw_artifact


def test_debug_url_filter_rejects_signed_or_connect_urls() -> None:
    assert StagehandAgent._safe_browserbase_debug_url(
        "https://www.browserbase.com/devtools/inspector.html?"
        "wss=connect.browserbase.com/debug/x"
    ) is None
    assert StagehandAgent._safe_browserbase_debug_url(
        "wss://connect.browserbase.com/debug/x"
    ) is None
    assert (
        StagehandAgent._safe_browserbase_debug_url(
            "https://browserbase.com/devtools/inspector.html"
        )
        == "https://browserbase.com/devtools/inspector.html"
    )


def test_no_id_warns_without_writing_artifact(
    agent_factory: Callable[..., StagehandAgent],
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = agent_factory()

    agent._publish_browserbase_session(
        context=AgentContext(),
        task_id=TASK_ID,
        result=ExecResult(stdout="ordinary eval output\r\n", return_code=1),
    )

    assert TASK_ID in caplog.text
    assert "No Browserbase session id" in caplog.text
    assert not (Path(agent.logs_dir) / "browserbase_session.json").exists()


def test_artifact_write_failure_is_swallowed(
    agent_factory: Callable[..., StagehandAgent],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = agent_factory()

    def fail_write_text(*args: object, **kwargs: object) -> int:
        del args, kwargs
        raise OSError("artifact write blocked")

    monkeypatch.setattr(Path, "write_text", fail_write_text)
    agent._publish_browserbase_session(
        context=AgentContext(),
        task_id=TASK_ID,
        result=ExecResult(stdout=SESSION_URL, return_code=0),
    )

    assert "Could not publish Browserbase session" in caplog.text
    assert "artifact write blocked" in caplog.text


def _successful_run_script(
    trajectory_dir: str,
) -> Callable[[str, dict[str, str]], ExecResult]:
    trajectory_path = f"{trajectory_dir}/trajectory.json"

    def script(command: str, env: dict[str, str]) -> ExecResult:
        del env
        if command == "evals --help":
            return ExecResult(stdout="usage", return_code=0)
        if command.startswith("evals run") and "--preview" in shlex.split(command):
            return ExecResult(
                stdout=f"  Target: {TASK_ID}  →  core (1 task)\n",
                return_code=0,
            )
        if command.startswith("script -q -e -c ") or (
            command.startswith("evals run")
            and "--preview" not in shlex.split(command)
        ):
            return ExecResult(stdout=REAL_CAPTURED_LINE + "\r\n", return_code=0)
        if command.startswith("find "):
            return ExecResult(stdout=f"1\t42\t{trajectory_path}\n", return_code=0)
        if command == f"cat {shlex.quote(trajectory_path)}":
            return ExecResult(
                stdout=json.dumps(
                    {
                        "steps": [{"action": "navigate"}],
                        "status": "success",
                        "usage": {"input_tokens": 1},
                    }
                ),
                return_code=0,
            )
        return ExecResult(return_code=0)

    return script


@pytest.mark.asyncio
async def test_run_wraps_only_eval_and_merges_session_metadata(
    tmp_path: Path,
    agent_factory: Callable[..., StagehandAgent],
) -> None:
    trajectory_dir = "/tmp/trajectories/group/agent/iframe_form/run"
    environment = RecordingBaseEnvironment(
        tmp_path,
        _successful_run_script(trajectory_dir),
        supports_session_capture=True,
    )
    environment.uses_browserbase = True
    agent = agent_factory(task_id=TASK_ID)
    context = AgentContext(metadata={"stagehand": {"existing": "kept"}})

    await agent.setup(environment)
    await agent.run(f"stagehand-task-id: {TASK_ID}", environment, context)

    commands = [call["command"] for call in environment.calls]
    assert "evals --help" in commands
    assert all(
        not command.startswith("script ")
        for command in commands
        if "--preview" in command
    )
    run_command = next(
        command
        for command in commands
        if command.startswith("script -q -e -c ")
    )
    run_argv = shlex.split(run_command)
    assert run_argv[:4] == ["script", "-q", "-e", "-c"]
    assert run_argv[-1] == "/dev/null"
    assert run_argv[4].startswith("stty cols 400; evals run agent/iframe_form ")
    assert "evals config set verbose true" in commands

    stagehand = context.metadata["stagehand"]
    assert stagehand["existing"] == "kept"
    assert stagehand["browserbase_session_id"] == SESSION_ID
    assert stagehand["browserbase_session_url"] == SESSION_URL
    assert stagehand["browserbase_session_ids"] == [SESSION_ID]
    assert stagehand["task_id"] == TASK_ID
    assert stagehand["mode"] == "dom"
    assert stagehand["trajectory_dir"] == trajectory_dir
    assert "trajectory_root" in stagehand
    assert "requested_trajectory_group" in stagehand


@pytest.mark.asyncio
async def test_missing_pty_capability_runs_plain_command(
    tmp_path: Path,
    agent_factory: Callable[..., StagehandAgent],
    caplog: pytest.LogCaptureFixture,
) -> None:
    trajectory_dir = "/tmp/trajectories/group/agent/iframe_form/plain-run"
    environment = RecordingBaseEnvironment(
        tmp_path,
        _successful_run_script(trajectory_dir),
        supports_session_capture=False,
    )
    agent = agent_factory()

    await agent.run(f"stagehand-task-id: {TASK_ID}", environment, AgentContext())

    commands = [call["command"] for call in environment.calls]
    plain_run = next(
        command
        for command in commands
        if command.startswith("evals run")
        and "--preview" not in shlex.split(command)
    )
    assert plain_run == agent._build_eval_command(TASK_ID, use_browserbase=False)
    assert not any(command.startswith("script -q -e") for command in commands)
    assert "requires script and stty" in caplog.text


@pytest.mark.asyncio
async def test_failed_eval_still_publishes_session_before_raising(
    tmp_path: Path,
    agent_factory: Callable[..., StagehandAgent],
) -> None:
    def script(command: str, env: dict[str, str]) -> ExecResult:
        del env
        if command.startswith("evals run") and "--preview" in shlex.split(command):
            return ExecResult(
                stdout=f"  Target: {TASK_ID}  →  core (1 task)\n",
                return_code=0,
            )
        if command.startswith("script -q -e -c "):
            return ExecResult(stdout=REAL_CAPTURED_LINE, return_code=7)
        return ExecResult(return_code=0)

    environment = RecordingBaseEnvironment(
        tmp_path,
        script,
        supports_session_capture=True,
    )
    agent = agent_factory()
    context = AgentContext()

    with pytest.raises(StagehandAgentFailedError, match="return code 7"):
        await agent.run(f"stagehand-task-id: {TASK_ID}", environment, context)

    artifact = json.loads(
        (Path(agent.logs_dir) / "browserbase_session.json").read_text()
    )
    assert artifact["session_id"] == SESSION_ID
    assert context.metadata["stagehand"]["browserbase_session_id"] == SESSION_ID
