from __future__ import annotations

import asyncio
import importlib
import json
import shlex
from pathlib import Path

import pytest
import yaml

import bb_harbor.env as env_module
from bb_harbor import (
    BrowserbaseDockerEnvironment,
    TRAJECTORIES_ROOT,
    TRAJECTORY_POINTER_PATH,
)
from bb_harbor.agent import StagehandAgent, StagehandAgentFailedError
from bb_harbor.env import BrowserbaseEnvironment
from bb_harbor.verifier import (
    DEFAULT_JUDGE_MODEL,
    StagehandVerifier,
    StagehandVerifierUnhealthyError,
    _SYNTHESIZED_JUDGE_FAILURE_DESCRIPTIONS,
    _SYNTHESIZED_JUDGE_FAILURE_REASONINGS,
    compute_criteria_fraction,
)
from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.job.config import JobConfig
from harbor.models.task.config import NetworkMode
from harbor.models.task.task import Task
from harbor.models.task.verifier_mode import VerifierEnvironmentMode

from conftest import FakeAsyncBrowserbase, FakeSessions, RecordingBaseEnvironment


REWARD_KEYS = {"reward", "outcome", "process", "criteria_earned_frac"}
INSTRUCTION = "stagehand-task-id: agent/columbia_tuition"


def test_harbor_contract_and_components_are_concrete():
    """Required assertion 1: pinned abstract contracts and concrete components."""

    assert set(BaseEnvironment.__abstractmethods__) == {
        "_validate_definition",
        "download_dir",
        "download_file",
        "exec",
        "start",
        "stop",
        "type",
        "upload_dir",
        "upload_file",
    }
    assert set(BaseAgent.__abstractmethods__) == {"name", "run", "setup", "version"}
    assert BrowserbaseEnvironment.__abstractmethods__ == frozenset()
    assert BrowserbaseDockerEnvironment is BrowserbaseEnvironment
    assert StagehandAgent.__abstractmethods__ == frozenset()
    assert StagehandVerifier.__abstractmethods__ == frozenset()


@pytest.mark.asyncio
async def test_browserbase_scopes_are_isolated_across_interleaved_tasks(
    browserbase_env_factory, agent_factory
):
    """Required assertion 2: concurrent task-local Browserbase session injection."""

    both_inside = asyncio.Event()
    entered = 0
    entered_lock = asyncio.Lock()

    async def script(command, merged_env):
        nonlocal entered
        if command.startswith("evals run"):
            async with entered_lock:
                entered += 1
                if entered == 2:
                    both_inside.set()
            await asyncio.wait_for(both_inside.wait(), timeout=1)
        if command.startswith("find "):
            session_id = merged_env.get("BROWSERBASE_SESSION_ID", "unknown")
            path = (
                f"{TRAJECTORIES_ROOT}/scope/actual/agent/columbia_tuition/"
                f"{session_id}/trajectory.json"
            )
            return ExecResult(stdout=f"1\t10\t{path}\n", return_code=0)
        return ExecResult(return_code=0)

    env_a = browserbase_env_factory(session_id="trial-a__env", script=script)
    env_b = browserbase_env_factory(session_id="trial-b__env", script=script)
    env_a.browserbase_session_id = "bb-a"
    env_a.browserbase_connect_url = "wss://a"
    env_b.browserbase_session_id = "bb-b"
    env_b.browserbase_connect_url = "wss://b"

    await asyncio.gather(
        agent_factory().run(INSTRUCTION, env_a, AgentContext()),
        agent_factory().run(INSTRUCTION, env_b, AgentContext()),
    )

    eval_a = next(call for call in env_a.calls if call["command"].startswith("evals run"))
    eval_b = next(call for call in env_b.calls if call["command"].startswith("evals run"))
    assert eval_a["env"]["BROWSERBASE_SESSION_ID"] == "bb-a"
    assert eval_b["env"]["BROWSERBASE_SESSION_ID"] == "bb-b"
    assert all(call["env"].get("BROWSERBASE_SESSION_ID") != "bb-b" for call in env_a.calls)
    assert all(call["env"].get("BROWSERBASE_SESSION_ID") != "bb-a" for call in env_b.calls)
    assert eval_a["env"]["VERIFIER_PERSIST_TRAJECTORIES"] == "1"
    assert eval_b["env"]["VERIFIER_PERSIST_TRAJECTORIES"] == "1"


@pytest.mark.asyncio
async def test_session_scope_is_task_local_on_a_shared_environment_instance(
    browserbase_env_factory,
):
    """Pin task-local overlays on one shared instance (required assertion 2)."""

    environment = browserbase_env_factory()
    both_inside = asyncio.Event()
    entered = 0
    entered_lock = asyncio.Lock()

    async def run_scoped(session_id: str, connect_url: str) -> None:
        nonlocal entered
        with environment.scoped_exec_env(
            {
                "BROWSERBASE_SESSION_ID": session_id,
                "BROWSERBASE_CONNECT_URL": connect_url,
            }
        ):
            async with entered_lock:
                entered += 1
                if entered == 2:
                    both_inside.set()
            await asyncio.wait_for(both_inside.wait(), timeout=1)
            await environment.exec(f"probe {session_id}")
            await asyncio.sleep(0)
            await environment.exec(f"probe {session_id} again")

    # The ContextVar is per-instance, so two instances cannot detect a task-local leak.
    await asyncio.gather(
        run_scoped("session-a", "wss://a"),
        run_scoped("session-b", "wss://b"),
    )

    calls_a = [call for call in environment.calls if "session-a" in call["command"]]
    calls_b = [call for call in environment.calls if "session-b" in call["command"]]
    assert len(calls_a) == 2
    assert len(calls_b) == 2
    for call in calls_a:
        assert call["env"]["BROWSERBASE_SESSION_ID"] == "session-a"
        assert call["env"]["BROWSERBASE_SESSION_ID"] != "session-b"
        assert call["env"]["BROWSERBASE_CONNECT_URL"] == "wss://a"
        assert call["env"]["BROWSERBASE_CONNECT_URL"] != "wss://b"
    for call in calls_b:
        assert call["env"]["BROWSERBASE_SESSION_ID"] == "session-b"
        assert call["env"]["BROWSERBASE_SESSION_ID"] != "session-a"
        assert call["env"]["BROWSERBASE_CONNECT_URL"] == "wss://b"
        assert call["env"]["BROWSERBASE_CONNECT_URL"] != "wss://a"


@pytest.mark.asyncio
async def test_session_scope_overlay_does_not_leak_into_a_concurrent_unscoped_task(
    browserbase_env_factory,
):
    """Required assertion 2 (discriminating): session_scope stays task-local."""

    environment = browserbase_env_factory()
    environment.browserbase_session_id = "scoped-session"
    environment.browserbase_connect_url = "wss://scoped"
    scope_entered = asyncio.Event()
    outside_recorded = asyncio.Event()

    async def scoped_task() -> None:
        with environment.session_scope():
            scope_entered.set()
            await asyncio.wait_for(outside_recorded.wait(), timeout=1)
            await environment.exec("inside-scope")

    async def unscoped_task() -> None:
        await asyncio.wait_for(scope_entered.wait(), timeout=1)
        await environment.exec("outside-scope")
        outside_recorded.set()

    await asyncio.gather(scoped_task(), unscoped_task())

    inside = next(
        call for call in environment.calls if call["command"] == "inside-scope"
    )
    outside = next(
        call for call in environment.calls if call["command"] == "outside-scope"
    )
    assert inside["env"]["BROWSERBASE_SESSION_ID"] == "scoped-session"
    assert "BROWSERBASE_SESSION_ID" not in outside["env"]


@pytest.mark.asyncio
async def test_session_scope_overlay_is_released_on_exit(browserbase_env_factory):
    """Required assertion 2: session_scope resets its overlay token on exit."""

    environment = browserbase_env_factory()
    environment.browserbase_session_id = "scoped-session"
    environment.browserbase_connect_url = "wss://scoped"

    with environment.session_scope():
        await environment.exec("inside-scope")
    await environment.exec("after-scope")

    inside = next(
        call for call in environment.calls if call["command"] == "inside-scope"
    )
    after = next(call for call in environment.calls if call["command"] == "after-scope")
    assert inside["env"]["BROWSERBASE_SESSION_ID"] == "scoped-session"
    assert "BROWSERBASE_SESSION_ID" not in after["env"]


@pytest.mark.asyncio
async def test_start_preserves_harbor_session_id(
    monkeypatch, browserbase_env_factory
):
    """Required assertion 3: Browserbase startup does not clobber Harbor identity."""

    fake_sessions = FakeSessions("sdk-session", "wss://sdk")

    class Client(FakeAsyncBrowserbase):
        def __init__(self, *, api_key):
            self.api_key = api_key
            self.sessions = fake_sessions

    async def docker_start(self, force_build):
        return None

    async def docker_stop(self, delete):
        return None

    monkeypatch.setenv("BROWSERBASE_API_KEY", "key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "project")
    monkeypatch.setattr(env_module, "AsyncBrowserbase", Client)
    monkeypatch.setattr(DockerEnvironment, "start", docker_start)
    monkeypatch.setattr(DockerEnvironment, "stop", docker_stop)
    env = browserbase_env_factory(session_id="trial-x__env")

    await env.start(force_build=False)
    assert env.session_id == "trial-x__env"
    assert env.browserbase_session_id == "sdk-session"
    assert fake_sessions.create_calls == [
        {"project_id": "project", "keep_alive": True}
    ]
    await env.stop(delete=True)


@pytest.mark.asyncio
async def test_stop_shields_release_from_task_cancellation(
    monkeypatch, browserbase_env_factory
):
    """Required assertion 4: shielded release under cancellation."""

    sessions = FakeSessions("sdk-session", "wss://sdk")
    sessions.update_started = asyncio.Event()
    sessions.update_gate = asyncio.Event()

    async def docker_stop(self, delete):
        return None

    monkeypatch.setattr(DockerEnvironment, "stop", docker_stop)
    env = browserbase_env_factory()
    env.browserbase_session_id = "sdk-session"
    env._browserbase_client = type("Client", (), {"sessions": sessions})()

    stop_task = asyncio.create_task(env.stop(delete=True))
    await asyncio.wait_for(sessions.update_started.wait(), timeout=1)
    stop_task.cancel()
    sessions.update_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await stop_task

    assert sessions.update_calls == [("sdk-session", "REQUEST_RELEASE")]
    assert sessions.update_completed is True
    assert env.browserbase_session_id is None
    await env.stop(delete=True)
    assert sessions.update_calls == [("sdk-session", "REQUEST_RELEASE")]


def test_session_env_requires_start_and_omits_missing_connect_url(
    browserbase_env_factory,
):
    """Environment contract: session export is guarded and omits falsy values."""

    environment = browserbase_env_factory()
    with pytest.raises(RuntimeError, match="call start"):
        environment.session_env()
    environment.browserbase_session_id = "bb-session"
    assert environment.session_env() == {"BROWSERBASE_SESSION_ID": "bb-session"}


def test_preflight_reports_all_missing_browserbase_variables(monkeypatch):
    """Environment contract: preflight names every missing Browserbase variable."""

    monkeypatch.setattr(
        DockerEnvironment, "preflight", classmethod(lambda cls: None)
    )
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    monkeypatch.delenv("BROWSERBASE_PROJECT_ID", raising=False)
    with pytest.raises(RuntimeError) as error:
        BrowserbaseEnvironment.preflight()
    assert "BROWSERBASE_API_KEY" in str(error.value)
    assert "BROWSERBASE_PROJECT_ID" in str(error.value)


@pytest.mark.asyncio
async def test_browserbase_startup_failure_tears_down_docker(
    monkeypatch, browserbase_env_factory
):
    """Environment contract: failed remote startup rolls Docker back."""

    teardown_calls = []

    class FailingSessions:
        async def create(self, **kwargs):
            raise RuntimeError("create failed")

    class Client:
        def __init__(self, *, api_key):
            self.sessions = FailingSessions()

    async def docker_start(self, force_build):
        return None

    async def docker_stop(self, delete):
        teardown_calls.append(delete)

    monkeypatch.setenv("BROWSERBASE_API_KEY", "key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "project")
    monkeypatch.setattr(env_module, "AsyncBrowserbase", Client)
    monkeypatch.setattr(DockerEnvironment, "start", docker_start)
    monkeypatch.setattr(DockerEnvironment, "stop", docker_stop)
    environment = browserbase_env_factory()

    with pytest.raises(RuntimeError, match="create failed"):
        await environment.start(force_build=False)
    assert teardown_calls == [True]


@pytest.mark.asyncio
async def test_agent_falls_back_to_browserbase_attributes_without_session_scope(
    tmp_path, agent_factory
):
    """Drift 2 regression: plain environments receive the compatibility overlay."""

    trajectory_dir = f"{TRAJECTORIES_ROOT}/plain/group/agent/columbia_tuition/run"

    def script(command, env):
        if command.startswith("find "):
            return ExecResult(
                stdout=f"1\t12\t{trajectory_dir}/trajectory.json\n", return_code=0
            )
        return ExecResult(return_code=0)

    environment = RecordingBaseEnvironment(tmp_path, script)
    environment.browserbase_session_id = "fallback-session"
    environment.browserbase_connect_url = "wss://fallback"
    await agent_factory().run(INSTRUCTION, environment, AgentContext())

    eval_call = next(
        call for call in environment.calls if call["command"].startswith("evals run")
    )
    assert eval_call["env"]["BROWSERBASE_SESSION_ID"] == "fallback-session"
    assert eval_call["env"]["BROWSERBASE_CONNECT_URL"] == "wss://fallback"
    assert "--env browserbase" in eval_call["command"]


@pytest.mark.asyncio
async def test_success_returns_all_reward_keys_and_builds_json_model_command(
    tmp_path, verifier_factory
):
    """Required assertion 5: successful verification emits the fixed reward keys."""

    payload = {"outcomeSuccess": True, "processScore": 0.5, "perCriterion": []}
    environment = RecordingBaseEnvironment(
        tmp_path, lambda command, env: ExecResult(stdout=json.dumps(payload), return_code=0)
    )
    result = await verifier_factory(environment).verify()

    assert set(result.rewards or {}) == REWARD_KEYS
    assert "reward" in (result.rewards or {})
    command = environment.calls[0]["command"]
    assert "--json" in command
    assert "--model" in command
    assert DEFAULT_JUDGE_MODEL != "gemini-2.5-flash"


def test_criteria_fraction_excludes_null_earned_points():
    """Required assertion 6: null earnedPoints affects neither sum nor denominator."""

    # Stagehand calls this field earnedPoints, not points.
    criteria = [
        {"earnedPoints": 1, "maxPoints": 2},
        {"earnedPoints": None, "maxPoints": 10},
        {"earnedPoints": 2, "maxPoints": 2},
    ]
    assert compute_criteria_fraction(criteria).fraction == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_verify_excludes_null_earned_points_end_to_end(
    tmp_path, verifier_factory
):
    """Required assertion 6: verify preserves the applicable-only fraction."""

    payload = {
        "outcomeSuccess": True,
        "perCriterion": [
            {"earnedPoints": 1, "maxPoints": 2},
            {"earnedPoints": None, "maxPoints": 10},
            {"earnedPoints": 2, "maxPoints": 2},
        ],
    }
    environment = RecordingBaseEnvironment(
        tmp_path, lambda command, env: ExecResult(stdout=json.dumps(payload), return_code=0)
    )
    result = await verifier_factory(environment).verify()
    assert result.rewards["criteria_earned_frac"] == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_synthesized_dead_judge_payloads_raise_unhealthy(
    tmp_path, verifier_factory
):
    """Required assertion 7: exact synthesized descriptions/reasonings are unhealthy."""

    description = next(iter(_SYNTHESIZED_JUDGE_FAILURE_DESCRIPTIONS))
    reasoning = next(iter(_SYNTHESIZED_JUDGE_FAILURE_REASONINGS))
    payloads = [
        {
            "outcomeSuccess": False,
            "findings": [
                {
                    "category": "verifier_uncertainty",
                    "description": description,
                    "message": "ignored",
                }
            ],
        },
        {"outcomeSuccess": False, "explanation": reasoning},
    ]
    for payload in payloads:
        environment = RecordingBaseEnvironment(
            tmp_path,
            lambda command, env, payload=payload: ExecResult(
                stdout=json.dumps(payload), return_code=0
            ),
        )
        with pytest.raises(StagehandVerifierUnhealthyError):
            await verifier_factory(environment).verify()


@pytest.mark.asyncio
async def test_ordinary_uncertainty_description_is_not_a_health_signature(
    tmp_path, verifier_factory
):
    """Required assertion 7: category alone and message fields do not trigger."""

    synthesized = next(iter(_SYNTHESIZED_JUDGE_FAILURE_DESCRIPTIONS))
    payload = {
        "outcomeSuccess": False,
        "findings": [
            {
                "category": "verifier_uncertainty",
                "description": "The page evidence was ambiguous.",
                "message": synthesized,
            }
        ],
    }
    environment = RecordingBaseEnvironment(
        tmp_path, lambda command, env: ExecResult(stdout=json.dumps(payload), return_code=0)
    )
    result = await verifier_factory(environment).verify()
    assert result.rewards["reward"] == 0.0


@pytest.mark.asyncio
async def test_legitimate_all_criteria_failure_returns_zero_with_all_keys(
    tmp_path, verifier_factory
):
    """Required assertion 8: genuine failure scores zero without unhealthy error."""

    payload = {
        "outcomeSuccess": False,
        "perCriterion": [
            {"earnedPoints": 0, "maxPoints": 2},
            {"earnedPoints": 0, "maxPoints": 3},
        ],
        "findings": [],
    }
    environment = RecordingBaseEnvironment(
        tmp_path, lambda command, env: ExecResult(stdout=json.dumps(payload), return_code=0)
    )
    result = await verifier_factory(environment).verify()
    assert result.rewards["reward"] == 0.0
    assert set(result.rewards) == REWARD_KEYS


@pytest.mark.asyncio
@pytest.mark.parametrize("find_stdout", ["", "1\t0\t/tmp/trajectory.json\n"])
async def test_agent_rejects_missing_or_empty_trajectory(
    find_stdout, browserbase_env_factory, agent_factory
):
    """Required assertion 9: successful eval without a nonempty trajectory fails."""

    def script(command, env):
        if command.startswith("find "):
            return ExecResult(stdout=find_stdout, return_code=0)
        return ExecResult(return_code=0)

    environment = browserbase_env_factory(script=script)
    environment.browserbase_session_id = "bb-session"
    environment.browserbase_connect_url = "wss://session"
    with pytest.raises(StagehandAgentFailedError):
        await agent_factory().run(INSTRUCTION, environment, AgentContext())


@pytest.mark.asyncio
async def test_agent_publishes_trajectory_metadata_and_pointer(
    browserbase_env_factory, agent_factory
):
    """Required assertion 9: happy path publishes metadata and pointer handshake."""

    trajectory_dir = (
        f"{TRAJECTORIES_ROOT}/trial/actual/agent/columbia_tuition/run-id"
    )

    def script(command, env):
        if command.startswith("find "):
            return ExecResult(
                stdout=f"1\t42\t{trajectory_dir}/trajectory.json\n", return_code=0
            )
        return ExecResult(return_code=0)

    environment = browserbase_env_factory(script=script)
    environment.browserbase_session_id = "bb-session"
    environment.browserbase_connect_url = "wss://session"
    agent = agent_factory()
    context = AgentContext()
    await agent.run(INSTRUCTION, environment, context)

    assert agent.trajectory_dir == trajectory_dir
    assert context.metadata["stagehand"]["trajectory_dir"] == trajectory_dir
    pointer_calls = [
        call for call in environment.calls if TRAJECTORY_POINTER_PATH in call["command"]
    ]
    assert len(pointer_calls) == 1
    assert "printf '%s\\n'" in pointer_calls[0]["command"]


def test_task_fixture_loads_with_real_harbor_loader():
    """Required assertion 10: Harbor validates and constructs the task fixture."""

    task_dir = Path(__file__).resolve().parents[1] / "tasks" / "wtb-smoke"
    assert Task.is_valid_dir(task_dir) is True
    task = Task(task_dir)
    assert task.config.environment.network_mode is NetworkMode.PUBLIC
    assert task.config.verifier.environment_mode is VerifierEnvironmentMode.SHARED
    assert task.config.metadata["suite"] == "wtb"
    assert task.config.metadata["task_id"] == "wtb-smoke"
    assert task.config.artifacts == []


def test_job_config_validates_and_import_paths_resolve(monkeypatch):
    """Additional assertion: job YAML validates and all custom imports resolve."""

    repo_root = Path(__file__).resolve().parents[1]
    payload = yaml.safe_load((repo_root / "job.yaml").read_text())
    monkeypatch.setenv("BROWSERBASE_API_KEY", "test-key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "test-project")
    config = JobConfig.model_validate(payload)

    import_paths = [
        config.environment.import_path,
        config.agents[0].import_path,
        config.verifier.import_path,
    ]
    expected = [BrowserbaseEnvironment, StagehandAgent, StagehandVerifier]
    for import_path, expected_class in zip(import_paths, expected, strict=True):
        module_name, class_name = import_path.split(":", 1)
        assert getattr(importlib.import_module(module_name), class_name) is expected_class

    assert config.environment.type is None
    assert config.n_concurrent_trials == 2
    assert config.tasks[0].path == Path("tasks/wtb-smoke")
    assert config.agents[0].env["BROWSERBASE_API_KEY"] == "${BROWSERBASE_API_KEY}"
    assert config.verifier.kwargs["judge_model"] == "google/gemini-3-flash-preview"


@pytest.mark.asyncio
async def test_verifier_prefers_pointer_before_discovery(tmp_path, verifier_factory):
    """Drift 3 regression: the verifier consumes the agent's pointer handshake."""

    pointer_dir = "/logs/agent/trajectories/from-pointer"

    def script(command, env):
        if command == f"cat {shlex.quote(TRAJECTORY_POINTER_PATH)}":
            return ExecResult(stdout=f"{pointer_dir}\n", return_code=0)
        if command.startswith("evals verify"):
            return ExecResult(stdout=json.dumps({"outcomeSuccess": True}), return_code=0)
        raise AssertionError(f"unexpected discovery command: {command}")

    environment = RecordingBaseEnvironment(tmp_path, script)
    verifier = verifier_factory(environment, trajectory_dir=None)
    await verifier.verify()
    assert pointer_dir in environment.calls[1]["command"]


@pytest.mark.asyncio
async def test_verifier_discovery_uses_deeper_layout_cap(tmp_path, verifier_factory):
    """Drift 3 regression: fallback discovery reaches the real nested layout."""

    trajectory_dir = f"{TRAJECTORIES_ROOT}/session/group/agent/task/run"

    def script(command, env):
        if command.startswith("find "):
            assert "-maxdepth 8" in command
            return ExecResult(stdout=f"{trajectory_dir}/trajectory.json\n", return_code=0)
        if command.startswith("evals verify"):
            return ExecResult(stdout=json.dumps({"outcomeSuccess": True}), return_code=0)
        raise AssertionError(command)

    environment = RecordingBaseEnvironment(tmp_path, script)
    verifier = verifier_factory(
        environment, trajectory_dir=None, trajectory_pointer_path=None
    )
    await verifier.verify()
