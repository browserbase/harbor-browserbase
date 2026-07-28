"""A Harbor Docker environment that targets Browserbase-backed Stagehand evals."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Generator
from typing import Any, override

from browserbase import AsyncBrowserbase
from harbor.environments.docker.docker import DockerEnvironment


SESSION_HEALTH_CHECK_INTERVAL_ENV = "BB_SESSION_HEALTH_CHECK_INTERVAL_SEC"
LEGACY_KEEPALIVE_INTERVAL_ENV = "BB_KEEPALIVE_INTERVAL_SEC"
DEFAULT_SESSION_HEALTH_CHECK_INTERVAL_SEC: float = 60.0


class BrowserbaseEnvironment(DockerEnvironment):
    """Pair a Harbor Docker container with a Browserbase Stagehand target.

    By default Stagehand creates and owns its Browserbase session. Opting into
    ``create_session=True`` pre-creates a session, but canonical Stagehand ignores
    ``BROWSERBASE_SESSION_ID`` and ``BROWSERBASE_CONNECT_URL``: the bench callers
    in ``packages/evals/framework/benchHarness.ts`` around lines 175 and 197 omit
    ``browserbaseSessionID``, while ``packages/evals/initV3.ts`` around line 118
    only accepts it through ``configOverrides``. The opt-in therefore double-bills
    with canonical Stagehand and is useful only with a build that plumbs that option.

    ``delete_on_start_failure`` is separate because Harbor passes the run-level
    ``delete`` setting to ``stop()`` only. ``self.task_env_config`` is the task-level
    model in ``harbor/models/task/config.py`` around line 416 and has no ``delete``
    field to inspect during ``start()``.

    The health-check task only observes session status. Browserbase ``keep_alive``
    already controls survival after disconnection; retrieving a session does not
    extend its lifetime.
    """

    uses_browserbase: bool = True

    def __init__(
        self,
        *args: Any,
        create_session: bool = False,
        delete_on_start_failure: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.create_session: bool = create_session
        self.delete_on_start_failure: bool = delete_on_start_failure
        self.browserbase_session_id: str | None = None
        self.browserbase_connect_url: str | None = None
        self._browserbase_client: AsyncBrowserbase | None = None
        self._session_health_check_task: asyncio.Task[None] | None = None

    @staticmethod
    @override
    def type() -> str:
        return "browserbase-docker"

    @classmethod
    @override
    def preflight(cls) -> None:
        super().preflight()
        missing = [
            name
            for name in ("BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID")
            if not os.environ.get(name, "").strip()
        ]
        if missing:
            raise RuntimeError(
                "Missing required Browserbase environment variable(s): "
                + ", ".join(missing)
            )

    def _client(self) -> AsyncBrowserbase:
        if self._browserbase_client is None:
            self._browserbase_client = AsyncBrowserbase(
                api_key=os.environ["BROWSERBASE_API_KEY"]
            )
        return self._browserbase_client

    async def _close_client(self) -> None:
        client = self._browserbase_client
        self._browserbase_client = None
        if client is None:
            return
        try:
            # browserbase/_base_client.py ~1480 exposes async close().
            await client.close()
        except BaseException:
            self.logger.exception("Failed to close the Browserbase client")

    def _record_created_session(self, response: Any) -> None:
        session_id = getattr(response, "id", None)
        if not session_id:
            raise RuntimeError("Browserbase session creation returned no session id")
        self.browserbase_session_id = str(session_id)
        connect_url = getattr(response, "connect_url", None)
        self.browserbase_connect_url = str(connect_url) if connect_url else None
        self.logger.info("Created Browserbase session %s", self.browserbase_session_id)

    @override
    async def start(self, force_build: bool) -> None:
        await super().start(force_build)
        if not self.create_session:
            self.logger.info(
                "Stagehand will create and own the Browserbase session for this trial"
            )
            return

        self.logger.warning(
            "Pre-creating a Browserbase session: canonical Stagehand ignores "
            "BROWSERBASE_SESSION_ID/BROWSERBASE_CONNECT_URL. This session is only "
            "consumable by a Stagehand build that plumbs browserbaseSessionID into "
            "initV3 configOverrides; canonical Stagehand will double-bill."
        )
        try:
            # SDK resources/sessions/sessions.py ~391 uses user_metadata; tagging with
            # Harbor's owned identity makes an unrecorded server-side create findable.
            create_task = asyncio.create_task(
                self._client().sessions.create(
                    project_id=os.environ["BROWSERBASE_PROJECT_ID"],
                    keep_alive=True,
                    user_metadata={"harborSessionId": self.session_id},
                )
            )
            try:
                response = await asyncio.shield(create_task)
            except asyncio.CancelledError:
                # Let an in-flight response land so its id can be released later.
                try:
                    response = await asyncio.shield(create_task)
                except BaseException:
                    pass
                else:
                    self._record_created_session(response)
                raise
            else:
                self._record_created_session(response)
                session_id = self.browserbase_session_id
                assert session_id is not None
                self._session_health_check_task = asyncio.create_task(
                    self._session_health_check(
                        session_id,
                        self._session_health_check_interval(),
                    )
                )
        except BaseException:
            if not self.browserbase_session_id:
                self.logger.error(
                    "Harbor session %s could not record a Browserbase session id; "
                    "possible Browserbase session leak",
                    self.session_id,
                )
            try:
                await super().stop(delete=self.delete_on_start_failure)
            except BaseException:
                self.logger.exception(
                    "Docker teardown also failed after Browserbase startup failure"
                )
            finally:
                await self._close_client()
            raise

    def session_env(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("BROWSERBASE_SESSION_ID", self.browserbase_session_id),
                ("BROWSERBASE_CONNECT_URL", self.browserbase_connect_url),
            )
            if value
        }

    @contextlib.contextmanager
    def session_scope(self) -> Generator[None, None, None]:
        """Inject any pre-created session values into this asyncio task's execs."""

        # BaseEnvironment.scoped_exec_env (base.py ~435) is ContextVar-backed and
        # intentionally accepts an empty mapping as a no-op.
        with self.scoped_exec_env(self.session_env()):
            yield

    async def _session_health_check(self, session_id: str, interval: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                response = await self._client().sessions.retrieve(session_id)
                status = getattr(response, "status", None)
                if status is not None and status != "RUNNING":
                    self.logger.warning(
                        "Browserbase session %s health check observed status %s; "
                        "stopping health checks",
                        session_id,
                        status,
                    )
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                # This background observer must never fail the trial.
                self.logger.exception(
                    "Browserbase health check failed for session %s", session_id
                )

    def _session_health_check_interval(self) -> float:
        raw_value = os.environ.get(SESSION_HEALTH_CHECK_INTERVAL_ENV)
        configured_name = SESSION_HEALTH_CHECK_INTERVAL_ENV
        if raw_value is None:
            # Preserve the previous environment variable as a compatibility alias.
            raw_value = os.environ.get(LEGACY_KEEPALIVE_INTERVAL_ENV)
            configured_name = LEGACY_KEEPALIVE_INTERVAL_ENV
        try:
            interval = (
                DEFAULT_SESSION_HEALTH_CHECK_INTERVAL_SEC
                if raw_value is None
                else float(raw_value)
            )
            if interval <= 0:
                raise ValueError
            return interval
        except (TypeError, ValueError):
            self.logger.warning(
                "Invalid %s value; using the %.0f-second default",
                configured_name,
                DEFAULT_SESSION_HEALTH_CHECK_INTERVAL_SEC,
            )
            return DEFAULT_SESSION_HEALTH_CHECK_INTERVAL_SEC

    async def _release(self, session_id: str) -> None:
        await self._client().sessions.update(session_id, status="REQUEST_RELEASE")

    @override
    async def stop(self, delete: bool) -> None:
        health_check_task = self._session_health_check_task
        self._session_health_check_task = None
        if health_check_task is not None:
            health_check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await health_check_task

        session_id = self.browserbase_session_id
        try:
            if session_id:
                release_task = asyncio.create_task(self._release(session_id))
                try:
                    await asyncio.shield(release_task)
                except asyncio.CancelledError:
                    try:
                        await asyncio.shield(release_task)
                    except Exception:
                        self.logger.exception(
                            "Browserbase release failed for session %s", session_id
                        )
                    else:
                        self.browserbase_session_id = None
                        self.browserbase_connect_url = None
                        self.logger.info("Released Browserbase session %s", session_id)
                    raise
                except Exception:
                    self.logger.exception(
                        "Browserbase release failed for session %s", session_id
                    )
                else:
                    self.browserbase_session_id = None
                    self.browserbase_connect_url = None
                    self.logger.info("Released Browserbase session %s", session_id)
        finally:
            try:
                await self._close_client()
            finally:
                await super().stop(delete)


BrowserbaseDockerEnvironment = BrowserbaseEnvironment

__all__ = [
    "BrowserbaseDockerEnvironment",
    "BrowserbaseEnvironment",
    "DEFAULT_SESSION_HEALTH_CHECK_INTERVAL_SEC",
    "LEGACY_KEEPALIVE_INTERVAL_ENV",
    "SESSION_HEALTH_CHECK_INTERVAL_ENV",
]
