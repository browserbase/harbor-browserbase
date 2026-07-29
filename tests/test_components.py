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
    StagehandVerifierEnvError,
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


REWARD_KEYS = {
    "reward",
    "outcome",
    "process",
    "process_measured",
    "criteria_earned_frac",
}
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

    env_a = browserbase_env_factory(
        session_id="trial-a__env", script=script, create_session=True
    )
    env_b = browserbase_env_factory(
        session_id="trial-b__env", script=script, create_session=True
    )
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

    environment = browserbase_env_factory(create_session=True)
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

    environment = browserbase_env_factory(create_session=True)
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
    clients = []

    class Client(FakeAsyncBrowserbase):
        def __init__(self, *, api_key):
            self.api_key = api_key
            self.sessions = fake_sessions
            clients.append(self)

    async def docker_start(self, force_build):
        return None

    async def docker_stop(self, delete):
        return None

    monkeypatch.setenv("BROWSERBASE_API_KEY", "key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "project")
    monkeypatch.setattr(env_module, "AsyncBrowserbase", Client)
    monkeypatch.setattr(DockerEnvironment, "start", docker_start)
    monkeypatch.setattr(DockerEnvironment, "stop", docker_stop)
    env = browserbase_env_factory(
        session_id="trial-x__env", create_session=True
    )

    await env.start(force_build=False)
    assert env.session_id == "trial-x__env"
    assert env.browserbase_session_id == "sdk-session"
    assert fake_sessions.create_calls == [
        {
            "project_id": "project",
            "keep_alive": True,
            "user_metadata": {"harborSessionId": "trial-x__env"},
        }
    ]
    await env.stop(delete=True)
    assert clients[0].close_calls == 1


@pytest.mark.asyncio
async def test_default_start_leaves_session_creation_to_stagehand(
    monkeypatch, browserbase_env_factory, agent_factory
):
    """Default startup consumes one Stagehand-owned Browserbase session."""

    sessions = FakeSessions()

    class Client(FakeAsyncBrowserbase):
        def __init__(self, *, api_key):
            self.api_key = api_key
            self.sessions = sessions

    async def docker_start(self, force_build):
        return None

    async def docker_stop(self, delete):
        return None

    trajectory_dir = f"{TRAJECTORIES_ROOT}/default/group/agent/columbia_tuition/run"

    def script(command, merged_env):
        if command.startswith("find "):
            return ExecResult(
                stdout=f"1\t12\t{trajectory_dir}/trajectory.json\n", return_code=0
            )
        return ExecResult(return_code=0)

    monkeypatch.setenv("BROWSERBASE_API_KEY", "key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "project")
    monkeypatch.setattr(env_module, "AsyncBrowserbase", Client)
    monkeypatch.setattr(DockerEnvironment, "start", docker_start)
    monkeypatch.setattr(DockerEnvironment, "stop", docker_stop)
    environment = browserbase_env_factory(script=script)

    await environment.start(force_build=False)
    await agent_factory().run(INSTRUCTION, environment, AgentContext())

    eval_call = next(
        call for call in environment.calls if call["command"].startswith("evals run")
    )
    assert sessions.create_calls == []
    assert environment.browserbase_session_id is None
    assert "BROWSERBASE_SESSION_ID" not in eval_call["env"]
    assert "BROWSERBASE_CONNECT_URL" not in eval_call["env"]
    assert "--env browserbase" in eval_call["command"]
    await environment.stop(delete=True)


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
    env = browserbase_env_factory(create_session=True)
    env.browserbase_session_id = "sdk-session"

    class Client:
        def __init__(self):
            self.sessions = sessions

        async def close(self):
            return None

    env._browserbase_client = Client()

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


@pytest.mark.asyncio
async def test_cancelled_create_records_session_for_later_release(
    monkeypatch, browserbase_env_factory
):
    """A cancellation cannot orphan an in-flight keep-alive session response."""

    sessions = FakeSessions("sdk-session", "wss://sdk")
    sessions.create_started = asyncio.Event()
    sessions.create_gate = asyncio.Event()

    class Client(FakeAsyncBrowserbase):
        def __init__(self, *, api_key):
            self.api_key = api_key
            self.sessions = sessions

    async def docker_start(self, force_build):
        return None

    async def docker_stop(self, delete):
        return None

    monkeypatch.setenv("BROWSERBASE_API_KEY", "key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "project")
    monkeypatch.setattr(env_module, "AsyncBrowserbase", Client)
    monkeypatch.setattr(DockerEnvironment, "start", docker_start)
    monkeypatch.setattr(DockerEnvironment, "stop", docker_stop)
    environment = browserbase_env_factory(create_session=True)

    start_task = asyncio.create_task(environment.start(force_build=False))
    await asyncio.wait_for(sessions.create_started.wait(), timeout=1)
    start_task.cancel()
    sessions.create_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert sessions.create_completed is True
    assert environment.browserbase_session_id == "sdk-session"
    await environment.stop(delete=True)
    assert sessions.update_calls == [("sdk-session", "REQUEST_RELEASE")]


def test_session_env_is_empty_before_start_and_omits_missing_connect_url(
    browserbase_env_factory,
):
    """Environment contract: an absent opt-in session is a valid no-op."""

    environment = browserbase_env_factory()
    assert environment.session_env() == {}
    with environment.session_scope():
        assert environment.session_env() == {}
    environment.browserbase_session_id = "bb-session"
    assert environment.session_env() == {"BROWSERBASE_SESSION_ID": "bb-session"}


@pytest.mark.asyncio
async def test_session_health_check_tolerates_unknown_then_stops_on_terminal_status(
    caplog, browserbase_env_factory
):
    """The background observer accepts old response shapes and logs terminal state."""

    class Sessions:
        def __init__(self):
            self.retrieve_calls = []

        async def retrieve(self, session_id):
            self.retrieve_calls.append(session_id)
            if len(self.retrieve_calls) == 1:
                return type("Response", (), {})()
            return type("Response", (), {"status": "COMPLETED"})()

    sessions = Sessions()
    environment = browserbase_env_factory()
    environment._browserbase_client = type("Client", (), {"sessions": sessions})()

    await asyncio.wait_for(
        environment._session_health_check("sdk-session", interval=0), timeout=1
    )

    assert sessions.retrieve_calls == ["sdk-session", "sdk-session"]
    assert "observed status COMPLETED" in caplog.text


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
    monkeypatch, browserbase_env_factory, caplog
):
    """Environment contract: failed remote startup rolls Docker back."""

    teardown_calls = []

    class FailingSessions:
        async def create(self, **kwargs):
            raise RuntimeError("create failed")

    class Client:
        def __init__(self, *, api_key):
            self.sessions = FailingSessions()

        async def close(self):
            return None

    async def docker_start(self, force_build):
        return None

    async def docker_stop(self, delete):
        teardown_calls.append(delete)

    monkeypatch.setenv("BROWSERBASE_API_KEY", "key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "project")
    monkeypatch.setattr(env_module, "AsyncBrowserbase", Client)
    monkeypatch.setattr(DockerEnvironment, "start", docker_start)
    monkeypatch.setattr(DockerEnvironment, "stop", docker_stop)
    environment = browserbase_env_factory(
        create_session=True, delete_on_start_failure=False
    )

    with pytest.raises(RuntimeError, match="create failed"):
        await environment.start(force_build=False)
    assert teardown_calls == [False]
    assert environment._browserbase_client is None
    assert "possible Browserbase session leak" in caplog.text


@pytest.mark.asyncio
async def test_agent_uses_explicit_browserbase_flag_without_session_scope(
    tmp_path, agent_factory
):
    """An explicit Browserbase environment receives the compatibility overlay."""

    trajectory_dir = f"{TRAJECTORIES_ROOT}/plain/group/agent/columbia_tuition/run"

    def script(command, env):
        if command.startswith("find "):
            return ExecResult(
                stdout=f"1\t12\t{trajectory_dir}/trajectory.json\n", return_code=0
            )
        return ExecResult(return_code=0)

    environment = RecordingBaseEnvironment(tmp_path, script)
    environment.uses_browserbase = True
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
async def test_agent_classifies_missing_precreated_session_id(
    browserbase_env_factory, agent_factory
):
    """A broken opt-in session contract raises the Harbor-classifiable error."""

    environment = browserbase_env_factory(create_session=True)
    with pytest.raises(StagehandAgentFailedError, match="no browserbase_session_id"):
        await agent_factory().run(INSTRUCTION, environment, AgentContext())


@pytest.mark.asyncio
async def test_callable_session_scope_does_not_imply_browserbase(
    tmp_path, agent_factory
):
    """A plain environment stays local even if it happens to expose a scope helper."""

    trajectory_dir = f"{TRAJECTORIES_ROOT}/plain/group/agent/columbia_tuition/run"

    def script(command, merged_env):
        if command.startswith("find "):
            return ExecResult(
                stdout=f"1\t12\t{trajectory_dir}/trajectory.json\n", return_code=0
            )
        return ExecResult(return_code=0)

    environment = RecordingBaseEnvironment(tmp_path, script)
    environment.session_scope = lambda: environment.scoped_exec_env(
        {"SHOULD_NOT_BE_INJECTED": "1"}
    )
    await agent_factory().run(INSTRUCTION, environment, AgentContext())

    eval_call = next(
        call for call in environment.calls if call["command"].startswith("evals run")
    )
    assert "--env browserbase" not in eval_call["command"]
    assert "SHOULD_NOT_BE_INJECTED" not in eval_call["env"]


@pytest.mark.asyncio
async def test_agent_forwards_present_provider_keys_without_inventing_absent_ones(
    tmp_path, agent_factory
):
    """Provider credentials come only from extra_env and omit absent aliases."""

    trajectory_dir = f"{TRAJECTORIES_ROOT}/plain/group/agent/columbia_tuition/run"

    def script(command, env):
        if command.startswith("find "):
            return ExecResult(
                stdout=f"1\t12\t{trajectory_dir}/trajectory.json\n", return_code=0
            )
        return ExecResult(return_code=0)

    with_key = RecordingBaseEnvironment(tmp_path, script, session_id="with-key")
    await agent_factory(extra_env={"GEMINI_API_KEY": "provider-key"}).run(
        INSTRUCTION, with_key, AgentContext()
    )
    eval_with_key = next(
        call for call in with_key.calls if call["command"].startswith("evals run")
    )
    assert eval_with_key["env"]["GEMINI_API_KEY"] == "provider-key"

    without_key = RecordingBaseEnvironment(tmp_path, script, session_id="without-key")
    await agent_factory(extra_env={}).run(INSTRUCTION, without_key, AgentContext())
    eval_without_key = next(
        call for call in without_key.calls if call["command"].startswith("evals run")
    )
    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        assert name not in eval_without_key["env"]


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
    assert result.rewards["process_measured"] == 1.0
    command = environment.calls[0]["command"]
    assert "--json" in command
    assert "--model" in command
    assert DEFAULT_JUDGE_MODEL != "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_verifier_exec_env_uses_override_precedence_and_none_when_empty(
    tmp_path, verifier_factory
):
    """Verifier exec receives Harbor's resolved env, including explicit None."""

    payload = {"outcomeSuccess": True}

    def script(command, env):
        return ExecResult(stdout=json.dumps(payload), return_code=0)

    configured_environment = RecordingBaseEnvironment(tmp_path, script)
    await verifier_factory(
        configured_environment,
        verifier_env={"GEMINI_API_KEY": "vk"},
        override_env={"GEMINI_API_KEY": "ok"},
    ).verify()
    assert configured_environment.calls[0]["raw_env"]["GEMINI_API_KEY"] == "ok"

    empty_environment = RecordingBaseEnvironment(tmp_path, script)
    await verifier_factory(empty_environment).verify()
    assert empty_environment.calls[0]["raw_env"] is None


@pytest.mark.asyncio
async def test_verifier_unresolved_env_names_offending_keys(
    monkeypatch, tmp_path, verifier_factory
):
    """Unresolved templates raise the integration error with destination keys."""

    monkeypatch.delenv("MISSING_STAGEHAND_TEST_KEY", raising=False)
    environment = RecordingBaseEnvironment(tmp_path)
    verifier = verifier_factory(
        environment,
        verifier_env={"GEMINI_API_KEY": "${MISSING_STAGEHAND_TEST_KEY}"},
    )
    with pytest.raises(StagehandVerifierEnvError, match="GEMINI_API_KEY"):
        await verifier.verify()


@pytest.mark.asyncio
async def test_verifier_prefers_persisted_result_without_fresh_judge(
    tmp_path, verifier_factory
):
    payload = {"outcomeSuccess": True, "processScore": 0.75}

    def script(command, env):
        if command == "cat /trajectory/run/scores/result.json":
            return ExecResult(stdout=json.dumps(payload), return_code=0)
        raise AssertionError(f"fresh verifier should not run: {command}")

    environment = RecordingBaseEnvironment(tmp_path, script)
    verifier = verifier_factory(environment, prefer_persisted_result=True)
    result = await verifier.verify()

    assert result.rewards["process"] == 0.75
    assert verifier.reward_source == "reward source: persisted scores/result.json"
    assert not any(
        call["command"].startswith("evals verify") for call in environment.calls
    )


@pytest.mark.asyncio
async def test_verifier_falls_back_when_persisted_result_is_missing(
    tmp_path, verifier_factory
):
    payload = {"outcomeSuccess": False}

    def script(command, env):
        if command.startswith("cat "):
            return ExecResult(stderr="missing", return_code=1)
        if command.startswith("evals verify"):
            return ExecResult(stdout=json.dumps(payload), return_code=0)
        raise AssertionError(command)

    environment = RecordingBaseEnvironment(tmp_path, script)
    verifier = verifier_factory(environment, prefer_persisted_result=True)
    result = await verifier.verify()

    assert result.rewards["reward"] == 0.0
    assert verifier.reward_source == "reward source: evals verify --json"
    assert [call["command"].split()[0] for call in environment.calls] == [
        "cat",
        "evals",
    ]


@pytest.mark.asyncio
async def test_persisted_dead_judge_result_is_still_unhealthy(
    tmp_path, verifier_factory
):
    payload = {
        "outcomeSuccess": False,
        "explanation": next(iter(_SYNTHESIZED_JUDGE_FAILURE_REASONINGS)),
    }
    environment = RecordingBaseEnvironment(
        tmp_path,
        lambda command, env: ExecResult(
            stdout=json.dumps(payload), return_code=0
        ),
    )
    verifier = verifier_factory(environment, prefer_persisted_result=True)

    with pytest.raises(StagehandVerifierUnhealthyError):
        await verifier.verify()
    assert verifier.reward_source == "reward source: persisted scores/result.json"


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
    assert result.rewards["process"] == 0.0
    assert result.rewards["process_measured"] == 0.0
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

    environment = browserbase_env_factory(script=script, create_session=True)
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

    environment = browserbase_env_factory(script=script, create_session=True)
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
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
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
    assert config.environment.kwargs == {
        "create_session": False,
        "delete_on_start_failure": True,
    }
    assert config.n_concurrent_trials == 3
    assert config.tasks == []
    assert len(config.datasets) == 1
    assert config.datasets[0].path == Path("tasks")
    assert config.agents[0].env["BROWSERBASE_API_KEY"] == "${BROWSERBASE_API_KEY}"
    assert config.agents[0].env["GEMINI_API_KEY"] == "${GEMINI_API_KEY}"
    assert config.verifier.env["GEMINI_API_KEY"] == "${GEMINI_API_KEY}"
    assert config.verifier.kwargs["judge_model"] == "google/gemini-3-flash-preview"


@pytest.mark.asyncio
async def test_job_config_datasets_discover_all_ten_fixtures(monkeypatch):
    """Guard against a fixture silently failing Task.is_valid_dir and shrinking the suite."""

    repo_root = Path(__file__).resolve().parents[1]
    payload = yaml.safe_load((repo_root / "job.yaml").read_text())
    config = JobConfig.model_validate(payload)
    monkeypatch.chdir(repo_root)

    task_configs = await config.datasets[0].get_task_configs()
    discovered_names = {task_config.path.name for task_config in task_configs}
    expected_names = {
        "agent-all-recipes",
        "agent-arxiv-gpt-report",
        "agent-github-react-version",
        "agent-github-ruby-repo",
        "agent-hugging-face",
        "agent-iframe-form",
        "agent-nba-trades",
        "agent-sf-library-card",
        "agent-thegamer-opinion",
        "wtb-smoke",
    }

    assert len(task_configs) == 10
    assert discovered_names == expected_names


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
