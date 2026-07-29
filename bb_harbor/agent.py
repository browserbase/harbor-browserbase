"""Host-side Harbor agent for running Stagehand evals in a task container.

``StagehandAgent`` invokes the container's ``evals run <target>`` command and
publishes the resulting trajectory location for a later verifier phase.

Harbor does not expose task TOML metadata to a custom ``BaseAgent``.  Task
identity therefore comes from an explicit ``stagehand-task-id: <id>`` marker
line in the instruction, with the constructor's ``task_id`` override taking
precedence.  Deriving the id from ``session_id`` was rejected: Harbor builds a
trial name from a task name truncated to 32 characters plus a short UUID, so
that reverse mapping is lossy and can silently select the wrong eval.

Container variables are applied with ``BaseEnvironment.scoped_exec_env`` and
never by mutating host ``os.environ``.  Harbor may run trials concurrently in
one process, making process-global mutation unsafe.  Browserbase and model-provider
credentials must be supplied through the agent's ``extra_env``, not
``[environment].env``:
``Trial._scrub_jobs_dir`` collects secrets from ``agent.extra_env`` (and the
task/run verifier envs), but not from environment env. Because
``BROWSERBASE_API_KEY`` matches Harbor's sensitive-key regex, its value is
redacted even when embedded in a signed connect URL. ``BROWSERBASE_PROJECT_ID``
does not match and survives verbatim; stripping it from shared jobs output is an
operator responsibility and a known gap.

The CLI currently replaces a caller-provided ``EVAL_TRAJECTORY_GROUP`` with a
run-tokenized group.  This agent still supplies the requested session-derived
group, but also uses a session-specific ``EVAL_TRAJECTORY_ROOT`` and discovers
the actual recorder-created group below it.  That preserves isolation between
concurrent Harbor trials despite the CLI override.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import Any, Final
from uuid import uuid4

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.agent.context import AgentContext

from bb_harbor import TRAJECTORIES_ROOT, TRAJECTORY_POINTER_PATH


class StagehandAgentFailedError(RuntimeError):
    """Raised when the Stagehand eval command or trajectory flush fails."""


class StagehandRolloutFailedError(StagehandAgentFailedError):
    """Raised when a flushed Stagehand trajectory declares ``status: error``.

    The recorder shape is evidenced by ``jobs/live-smoke-a4/.../agent/``
    ``columbia_tuition/2026-07-29T00-14-21-050Z/trajectory.json`` around lines
    16-20: ``steps``, ``status``, and usage token counts are top-level fields.
    """


class StagehandAgentTaskIdError(StagehandAgentFailedError):
    """Raised when no safe, explicit Stagehand task identity is available."""


_VALID_MODES: Final[tuple[str, ...]] = ("dom", "hybrid", "cua")
_TASK_ID_VALUE: Final[str] = r"[A-Za-z0-9._\-/]+"
_TASK_ID_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t]*stagehand-task-id[ \t]*:[ \t]*(?P<task_id>{_TASK_ID_VALUE})[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_FULL_TASK_ID_RE: Final[re.Pattern[str]] = re.compile(rf"^{_TASK_ID_VALUE}$")
_TASK_ID_MARKER: Final[str] = "stagehand-task-id: <task-id>"
_DIAGNOSTIC_BLOB_LIMIT: Final[int] = 2_000
_ERROR_CARRIER_KEYS: Final[tuple[str, ...]] = (
    "error",
    "errorMessage",
    "error_message",
    "message",
    "failureReason",
)
# Stagehand core utils.ts ~690 defines all Google aliases in this precedence;
# v3Evaluator.ts ~76 directly consumes the first two for verifier judging.
_FORWARDED_EXTRA_ENV_NAMES: Final[tuple[str, ...]] = (
    "BROWSERBASE_API_KEY",
    "BROWSERBASE_PROJECT_ID",
    "GEMINI_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GOOGLE_API_KEY",
)


class StagehandAgent(BaseAgent):
    """Drive one Stagehand eval inside a Harbor-managed container.

    Put Browserbase credentials and the selected model provider key in the
    agent's ``extra_env``. In addition to safely scoping them to agent execution,
    this lets ``Trial._scrub_jobs_dir`` redact values whose key names are
    sensitive; ``[environment].env`` is not scanned. The API key qualifies, but
    the project id does not.
    """

    def __init__(
        self,
        *args: Any,
        mode: str = "dom",
        task_id: str | None = None,
        logs_root: str = posixpath.dirname(TRAJECTORIES_ROOT),
        timeout_sec: int | None = None,
        evals_version: str | None = None,
        evals_package_json_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        if mode not in _VALID_MODES:
            valid = ", ".join(_VALID_MODES)
            raise ValueError(f"Invalid Stagehand mode {mode!r}; valid values: {valid}")
        if timeout_sec is not None and timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive or None")

        super().__init__(*args, **kwargs)
        self.mode: str = mode
        self.task_id: str | None = task_id
        self.logs_root: str = logs_root
        self.timeout_sec: int | None = timeout_sec
        self.evals_version: str | None = evals_version
        self.evals_package_json_path: Path | None = (
            Path(evals_package_json_path) if evals_package_json_path is not None else None
        )
        self.trajectory_dir: str | None = None

        self._version_resolved: bool = False
        self._cached_version: str | None = None
        self._instance_id: str = uuid4().hex

    @staticmethod
    def name() -> str:
        """Return Harbor's stable identifier for this agent."""

        return "stagehand"

    def version(self) -> str | None:
        """Return the host-installed evals package version, if discoverable."""

        if self._version_resolved:
            return self._cached_version

        resolved: str | None = None
        try:
            explicit = self.evals_version.strip() if self.evals_version else ""
            from_env = os.environ.get("STAGEHAND_EVALS_VERSION", "").strip()
            if explicit:
                resolved = explicit
            elif from_env:
                resolved = from_env
            elif self.evals_package_json_path is not None:
                payload: object = json.loads(
                    self.evals_package_json_path.read_text(encoding="utf-8")
                )
                if isinstance(payload, dict):
                    value = payload.get("version")
                    if isinstance(value, str) and value.strip():
                        resolved = value.strip()
        except Exception:
            resolved = None

        self._cached_version = resolved
        self._version_resolved = True
        return resolved

    async def setup(self, environment: BaseEnvironment) -> None:
        """Verify that the non-interactive evals help path is available."""

        self.logger.info("Checking for the Stagehand evals CLI")
        result = await environment.exec("evals --help", timeout_sec=self.timeout_sec)
        if result.return_code != 0:
            raise StagehandAgentFailedError(
                "Stagehand evals CLI probe `evals --help` failed with return code "
                f"{result.return_code}. stderr: {result.stderr or ''!r}; "
                f"stdout: {result.stdout or ''!r}"
            )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Run the selected eval and publish its fully flushed trajectory."""

        resolved_task_id = self._resolve_task_id(instruction)
        session_value = self._session_value(environment)
        session_slug = self._sanitize_slug(session_value)
        if not session_slug:
            session_slug = f"stagehand-{self._instance_id}"
            self.logger.warning(
                "No usable Harbor session_id was available; using an instance-unique "
                "trajectory scope"
            )

        trajectory_root = posixpath.join(
            self.logs_root.rstrip("/") or "/", "trajectories", session_slug
        )
        requested_group = session_slug
        mkdir_result = await environment.exec(
            f"mkdir -p {shlex.quote(trajectory_root)}",
            timeout_sec=self.timeout_sec,
        )
        if mkdir_result.return_code != 0:
            raise StagehandAgentFailedError(
                f"Could not create trajectory root {trajectory_root!r}; return code "
                f"{mkdir_result.return_code}; stderr: {mkdir_result.stderr or ''!r}; "
                f"stdout: {mkdir_result.stdout or ''!r}"
            )

        # Browserbase intent is explicit. Attribute presence is not a contract and
        # canonical Docker environments must remain local (FINDING J).
        uses_browserbase = bool(getattr(environment, "uses_browserbase", False))
        browserbase_session_id = self._first_environment_value(
            environment, ("browserbase_session_id",)
        )
        browserbase_connect_url = self._first_environment_value(
            environment, ("browserbase_connect_url",)
        )
        precreated_session_requested = bool(
            getattr(environment, "create_session", False)
            or browserbase_connect_url
        )
        if (
            uses_browserbase
            and precreated_session_requested
            and not browserbase_session_id
        ):
            raise StagehandAgentFailedError(
                "The environment advertises a pre-created Browserbase session but "
                "has no browserbase_session_id"
            )
        if not uses_browserbase:
            self.logger.warning(
                "No Browserbase session id or connect URL was found on the environment; "
                "continuing as a local-environment eval"
            )

        scoped_env = self._build_scoped_env(
            trajectory_root=trajectory_root,
            trajectory_group=requested_group,
        )
        command = self._build_eval_command(
            resolved_task_id, use_browserbase=uses_browserbase
        )
        self.logger.info(
            "Running Stagehand eval task %s in %s mode", resolved_task_id, self.mode
        )
        with ExitStack() as stack:
            if uses_browserbase and browserbase_session_id:
                session_scope = getattr(environment, "session_scope", None)
                if callable(session_scope):
                    try:
                        stack.enter_context(session_scope())
                    except RuntimeError as exc:
                        raise StagehandAgentFailedError(
                            "Could not enter the pre-created Browserbase session scope"
                        ) from exc
                else:
                    # Compatibility path for explicit Browserbase environments that
                    # expose session fields but not the ContextVar-backed helper.
                    scoped_env["BROWSERBASE_SESSION_ID"] = browserbase_session_id
                    if browserbase_connect_url:
                        scoped_env["BROWSERBASE_CONNECT_URL"] = browserbase_connect_url
            stack.enter_context(environment.scoped_exec_env(scoped_env))
            result = await environment.exec(command, timeout_sec=self.timeout_sec)

        if result.return_code != 0:
            raise StagehandAgentFailedError(
                f"Stagehand eval {resolved_task_id!r} failed with return code "
                f"{result.return_code}; stderr: {result.stderr or ''!r}; "
                f"stdout: {result.stdout or ''!r}"
            )

        trajectory_dir = await self._locate_flushed_trajectory(
            environment=environment,
            trajectory_root=trajectory_root,
            task_id=resolved_task_id,
        )
        self.trajectory_dir = trajectory_dir
        context.metadata = {
            **(context.metadata or {}),
            "stagehand": {
                "task_id": resolved_task_id,
                "mode": self.mode,
                "trajectory_dir": trajectory_dir,
                "trajectory_root": trajectory_root,
                "requested_trajectory_group": requested_group,
            },
        }
        # Publish the location in trial metadata for failure diagnosis, but inspect
        # the recorder's status before writing the success pointer or success log.
        await self._raise_if_trajectory_errored(
            environment=environment,
            trajectory_dir=trajectory_dir,
            task_id=resolved_task_id,
            eval_result=result,
        )
        pointer_parent = posixpath.dirname(TRAJECTORY_POINTER_PATH)
        pointer_command = (
            f"mkdir -p {shlex.quote(pointer_parent)} && "
            f"printf '%s\\n' {shlex.quote(trajectory_dir)} > "
            f"{shlex.quote(TRAJECTORY_POINTER_PATH)}"
        )
        pointer_result = await environment.exec(
            pointer_command, timeout_sec=self.timeout_sec
        )
        if pointer_result.return_code != 0:
            raise StagehandAgentFailedError(
                f"Could not write trajectory pointer {TRAJECTORY_POINTER_PATH!r}; "
                f"return code {pointer_result.return_code}; stderr: "
                f"{pointer_result.stderr or ''!r}; stdout: "
                f"{pointer_result.stdout or ''!r}"
            )
        self.logger.info("Stagehand trajectory flushed at %s", trajectory_dir)

    async def _raise_if_trajectory_errored(
        self,
        *,
        environment: BaseEnvironment,
        trajectory_dir: str,
        task_id: str,
        eval_result: ExecResult,
    ) -> None:
        """Fail only when readable recorder output positively declares an error.

        ``harbor/environments/base.py`` around line 56 defines ``ExecResult`` with
        ``return_code``, ``stdout``, and ``stderr``; all three are retained here so
        a zero-exit CLI failure still leaves actionable Harbor trial diagnostics.
        """

        trajectory_path = posixpath.join(trajectory_dir, "trajectory.json")
        command = f"cat {shlex.quote(trajectory_path)}"
        try:
            result = await environment.exec(command, timeout_sec=self.timeout_sec)
        except Exception as exc:
            self.logger.warning(
                "Could not read Stagehand trajectory status from %s: %s; continuing",
                trajectory_path,
                exc,
            )
            return

        if result.return_code != 0:
            self.logger.warning(
                "Could not read Stagehand trajectory status from %s; return code %s; "
                "stderr: %r; stdout: %r; continuing",
                trajectory_path,
                result.return_code,
                result.stderr or "",
                result.stdout or "",
            )
            return

        raw_payload = result.stdout or ""
        if not raw_payload.strip():
            self.logger.warning(
                "Stagehand trajectory %s was empty while checking status; continuing",
                trajectory_path,
            )
            return

        try:
            payload: object = json.loads(raw_payload)
        except (TypeError, ValueError) as exc:
            self.logger.warning(
                "Could not parse Stagehand trajectory status from %s: %s; continuing",
                trajectory_path,
                exc,
            )
            return

        if not isinstance(payload, dict):
            self.logger.warning(
                "Stagehand trajectory %s was not a JSON object while checking status; "
                "continuing",
                trajectory_path,
            )
            return

        status = payload.get("status")
        if not isinstance(status, str) or not status.strip():
            self.logger.warning(
                "Stagehand trajectory %s had no string status; continuing",
                trajectory_path,
            )
            return
        normalized_status = status.strip()
        if normalized_status.lower() != "error":
            return

        details = [
            f"task_id={task_id!r}",
            f"mode={self.mode!r}",
            f"trajectory_path={trajectory_path!r}",
            f"status={normalized_status!r}",
        ]
        steps = payload.get("steps")
        if isinstance(steps, list):
            details.append(f"step_count={len(steps)}")
        usage = payload.get("usage")
        if isinstance(usage, dict):
            if "input_tokens" in usage:
                details.append(f"usage.input_tokens={usage['input_tokens']!r}")
            if "output_tokens" in usage:
                details.append(f"usage.output_tokens={usage['output_tokens']!r}")

        diagnostics = self._trajectory_error_diagnostics(payload)
        if diagnostics:
            details.append("trajectory diagnostics: " + " | ".join(diagnostics))
        if eval_result.stdout:
            details.append(self._captured_stream_diagnostic("stdout", eval_result.stdout))
        if eval_result.stderr:
            details.append(self._captured_stream_diagnostic("stderr", eval_result.stderr))

        raise StagehandRolloutFailedError(
            "Stagehand eval exited successfully, but its flushed trajectory declared "
            "an errored rollout; "
            + "; ".join(details)
        )

    @classmethod
    def _trajectory_error_diagnostics(cls, payload: dict[str, Any]) -> list[str]:
        """Extract common recorder/agent error carriers without assuming one schema."""

        scopes: list[tuple[str, dict[str, Any]]] = [("trajectory", payload)]
        top_error = payload.get("error")
        if isinstance(top_error, dict):
            scopes.append(("trajectory.error", top_error))

        steps = payload.get("steps")
        if isinstance(steps, list) and steps and isinstance(steps[-1], dict):
            last_step = steps[-1]
            scopes.append(("last_step", last_step))
            step_error = last_step.get("error")
            if isinstance(step_error, dict):
                scopes.append(("last_step.error", step_error))

        diagnostics: list[str] = []
        seen: set[tuple[str, str]] = set()
        for scope_name, scope in scopes:
            for key in _ERROR_CARRIER_KEYS:
                if key not in scope or scope[key] is None:
                    continue
                normalized = cls._normalize_diagnostic_blob(scope[key])
                if not normalized:
                    continue
                identity = (key, normalized)
                if identity in seen:
                    continue
                seen.add(identity)
                diagnostics.append(
                    f"{scope_name}.{key}: " + cls._truncate_head(normalized)
                )
        return diagnostics

    @staticmethod
    def _normalize_diagnostic_blob(value: object) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            named_parts = [
                f"{key}={value[key]}"
                for key in ("name", "message", "stack")
                if value.get(key) is not None and str(value[key]).strip()
            ]
            if named_parts:
                return "; ".join(named_parts)
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value).strip()

    @staticmethod
    def _truncate_head(value: str) -> str:
        if len(value) <= _DIAGNOSTIC_BLOB_LIMIT:
            return value
        return value[:_DIAGNOSTIC_BLOB_LIMIT] + "\n... (truncated)\n[kept head]"

    @staticmethod
    def _captured_stream_diagnostic(name: str, value: str) -> str:
        if len(value) <= _DIAGNOSTIC_BLOB_LIMIT:
            return f"eval {name}: {value}"
        tail = value[-_DIAGNOSTIC_BLOB_LIMIT:]
        return f"eval {name}: ... (truncated)\n[kept tail]\n{tail}"

    def _resolve_task_id(self, instruction: str) -> str:
        raw_task_id = self.task_id
        if raw_task_id is None:
            match = _TASK_ID_RE.search(instruction)
            raw_task_id = match.group("task_id") if match else None

        if raw_task_id is None:
            raise StagehandAgentTaskIdError(
                "No Stagehand task id was provided. Add an exact marker line to the "
                f"instruction, for example `{_TASK_ID_MARKER}`, or pass the task_id "
                "agent constructor kwarg. Refusing to guess a task."
            )

        normalized = raw_task_id.strip()
        path_parts = PurePosixPath(normalized).parts
        if (
            not _FULL_TASK_ID_RE.fullmatch(normalized)
            or normalized.startswith("/")
            or any(part in {"", ".", ".."} for part in path_parts)
        ):
            raise StagehandAgentTaskIdError(
                f"Unsafe or invalid Stagehand task id {raw_task_id!r}. Use "
                f"`{_TASK_ID_MARKER}` with only letters, numbers, '.', '_', '-', "
                "and non-traversing '/' separators."
            )
        return normalized

    def _session_value(self, environment: BaseEnvironment) -> str:
        for value in (self.session_id, getattr(environment, "session_id", None)):
            if value is not None and str(value).strip():
                return str(value)
        return ""

    @staticmethod
    def _sanitize_slug(value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("_")
        return "" if re.fullmatch(r"\.+", slug) else slug

    def _first_environment_value(
        self,
        environment: BaseEnvironment,
        names: tuple[str, ...],
    ) -> str | None:
        for name in names:
            try:
                value = getattr(environment, name, None)
            except Exception as exc:
                self.logger.debug(
                    "Could not read environment attribute %s: %s", name, exc
                )
                continue
            if value is not None and str(value).strip():
                return str(value)
        return None

    def _build_scoped_env(
        self,
        *,
        trajectory_root: str,
        trajectory_group: str,
    ) -> dict[str, str]:
        scoped_env = {
            "EVAL_TRAJECTORY_ROOT": trajectory_root,
            "EVAL_TRAJECTORY_GROUP": trajectory_group,
            "VERIFIER_PERSIST_TRAJECTORIES": "1",
        }
        for name in _FORWARDED_EXTRA_ENV_NAMES:
            value = self.extra_env.get(name)
            if value:
                scoped_env[name] = value
        return scoped_env

    def _build_eval_command(self, task_id: str, *, use_browserbase: bool) -> str:
        argv = [
            "evals",
            "run",
            task_id,
            "--trials",
            "1",
            "--concurrency",
            "1",
            "--agent-mode",
            self.mode,
        ]
        if use_browserbase:
            argv.extend(("--env", "browserbase"))
        if self.model_name is not None:
            argv.extend(("--model", self.model_name))
        return " ".join(shlex.quote(value) for value in argv)

    async def _locate_flushed_trajectory(
        self,
        *,
        environment: BaseEnvironment,
        trajectory_root: str,
        task_id: str,
    ) -> str:
        pattern = f"*/{task_id}/*/trajectory.json"
        stat_format = "%Y\t%s\t%n"
        command = (
            f"find {shlex.quote(trajectory_root)} -type f "
            f"-path {shlex.quote(pattern)} -exec stat -c "
            f"{shlex.quote(stat_format)} -- {{}} +"
        )
        result: ExecResult = await environment.exec(
            command, timeout_sec=self.timeout_sec
        )
        if result.return_code != 0:
            raise StagehandAgentFailedError(
                "Failed while checking for a flushed Stagehand trajectory under "
                f"{trajectory_root!r} with task path {task_id!r}; return code "
                f"{result.return_code}; stderr: {result.stderr or ''!r}; "
                f"stdout: {result.stdout or ''!r}"
            )

        discovered: list[tuple[int, int, str]] = []
        malformed: list[str] = []
        for line in (result.stdout or "").splitlines():
            fields = line.split("\t", maxsplit=2)
            if len(fields) != 3:
                malformed.append(line)
                continue
            try:
                discovered.append((int(fields[0]), int(fields[1]), fields[2]))
            except ValueError:
                malformed.append(line)

        nonempty = [entry for entry in discovered if entry[1] > 0]
        if not nonempty:
            found = [f"{path} ({size} bytes)" for _, size, path in discovered]
            detail = ", ".join(found) if found else "no trajectory.json files"
            if malformed:
                detail += f"; unparseable stat output: {malformed!r}"
            raise StagehandAgentFailedError(
                "Stagehand eval returned successfully but no non-empty trajectory was "
                f"flushed. Looked below {trajectory_root!r} for pattern {pattern!r}; "
                f"found {detail}."
            )

        _, _, trajectory_path = max(nonempty, key=lambda entry: (entry[0], entry[2]))
        return posixpath.dirname(trajectory_path)
