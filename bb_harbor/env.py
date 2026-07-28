"""A Harbor Docker environment backed by a remote Browserbase session."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Generator
from typing import Any, override

from browserbase import AsyncBrowserbase
from harbor.environments.docker.docker import DockerEnvironment


KEEPALIVE_INTERVAL_ENV = "BB_KEEPALIVE_INTERVAL_SEC"
DEFAULT_KEEPALIVE_INTERVAL_SEC: float = 60.0

class BrowserbaseEnvironment(DockerEnvironment):
    """Pair a Harbor Docker container with one remote Browserbase session."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.browserbase_session_id: str | None = None
        self.browserbase_connect_url: str | None = None
        self._browserbase_client: AsyncBrowserbase | None = None
        self._keepalive_task: asyncio.Task[None] | None = None

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

    @override
    async def start(self, force_build: bool) -> None:
        await super().start(force_build)
        try:
            response = await self._client().sessions.create(
                project_id=os.environ["BROWSERBASE_PROJECT_ID"],
                keep_alive=True,
            )
            self.browserbase_session_id = response.id
            self.browserbase_connect_url = response.connect_url
            self.logger.info(
                "Created Browserbase session %s", self.browserbase_session_id
            )
            self._keepalive_task = asyncio.create_task(
                self._keepalive(
                    self.browserbase_session_id,
                    self._keepalive_interval(),
                )
            )
        except BaseException:
            try:
                await super().stop(delete=True)
            except BaseException:
                self.logger.exception(
                    "Docker teardown also failed after Browserbase startup failure"
                )
            raise

    def session_env(self) -> dict[str, str]:
        if not self.browserbase_session_id:
            raise RuntimeError(
                "Browserbase session is unavailable; call start() successfully first."
            )
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
        """Inject session values; enter on the asyncio task that later calls exec."""

        with self.scoped_exec_env(self.session_env()):
            yield

    async def _keepalive(self, session_id: str, interval: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                await self._client().sessions.retrieve(session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception(
                    "Browserbase keepalive failed for session %s", session_id
                )

    def _keepalive_interval(self) -> float:
        raw_value = os.environ.get(KEEPALIVE_INTERVAL_ENV)
        try:
            interval = (
                DEFAULT_KEEPALIVE_INTERVAL_SEC
                if raw_value is None
                else float(raw_value)
            )
            if interval <= 0:
                raise ValueError
            return interval
        except (TypeError, ValueError):
            self.logger.warning(
                "Invalid %s value; using the %.0f-second default",
                KEEPALIVE_INTERVAL_ENV,
                DEFAULT_KEEPALIVE_INTERVAL_SEC,
            )
            return DEFAULT_KEEPALIVE_INTERVAL_SEC

    async def _release(self, session_id: str) -> None:
        await self._client().sessions.update(session_id, status="REQUEST_RELEASE")

    @override
    async def stop(self, delete: bool) -> None:
        keepalive_task = self._keepalive_task
        self._keepalive_task = None
        if keepalive_task is not None:
            keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive_task

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
            await super().stop(delete)


BrowserbaseDockerEnvironment = BrowserbaseEnvironment

__all__ = [
    "BrowserbaseDockerEnvironment",
    "BrowserbaseEnvironment",
    "DEFAULT_KEEPALIVE_INTERVAL_SEC",
    "KEEPALIVE_INTERVAL_ENV",
]
