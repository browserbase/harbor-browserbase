from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx
import pytest
from browserbase import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    PermissionDeniedError,
)

import bb_harbor.env as env_module
from bb_harbor.env import BrowserbaseCredentialError, BrowserbaseEnvironment
from harbor.environments.docker.docker import DockerEnvironment


class _FakeProjects:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.retrieve_calls: list[str] = []

    def retrieve(self, project_id: str) -> object:
        self.retrieve_calls.append(project_id)
        if self.error is not None:
            raise self.error
        return object()


class _FakeBrowserbaseClient:
    def __init__(self, error: Exception | None, **kwargs: Any) -> None:
        self.kwargs: dict[str, Any] = kwargs
        self.projects: _FakeProjects = _FakeProjects(error)
        self.entered: bool = False
        self.exited: bool = False
        self.closed: bool = False

    def __enter__(self) -> _FakeBrowserbaseClient:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.exited = True
        self.closed = True


def _status_error(
    error_type: type[APIStatusError],
    status_code: int,
) -> APIStatusError:
    request = httpx.Request("GET", "https://api.browserbase.com/v1/projects/project-id")
    response = httpx.Response(status_code, request=request)
    return error_type(
        f"HTTP {status_code}",
        response=response,
        body={"message": "preflight failure"},
    )


def _prepare_preflight(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception | None,
) -> list[_FakeBrowserbaseClient]:
    clients: list[_FakeBrowserbaseClient] = []

    def fake_browserbase(**kwargs: Any) -> _FakeBrowserbaseClient:
        client = _FakeBrowserbaseClient(error, **kwargs)
        clients.append(client)
        return client

    def docker_preflight(cls: type[DockerEnvironment]) -> None:
        del cls

    monkeypatch.setenv("BROWSERBASE_API_KEY", "api-key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "project-id")
    monkeypatch.delenv("BB_SKIP_PREFLIGHT_API_CHECK", raising=False)
    monkeypatch.delenv("BROWSERBASE_BASE_URL", raising=False)
    monkeypatch.setattr(
        DockerEnvironment,
        "preflight",
        classmethod(docker_preflight),
    )
    monkeypatch.setattr(env_module, "Browserbase", fake_browserbase)
    return clients


@pytest.mark.parametrize(
    ("error_type", "status_code"),
    [
        (AuthenticationError, 401),
        (PermissionDeniedError, 403),
        (APIStatusError, 404),
    ],
)
def test_preflight_classifies_rejected_credentials_and_project(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[APIStatusError],
    status_code: int,
) -> None:
    clients = _prepare_preflight(
        monkeypatch,
        _status_error(error_type, status_code),
    )

    with pytest.raises(BrowserbaseCredentialError):
        BrowserbaseEnvironment.preflight()

    assert clients[0].projects.retrieve_calls == ["project-id"]
    assert clients[0].exited is True


@pytest.mark.parametrize(
    "error",
    [
        _status_error(APIStatusError, 500),
        APIConnectionError(
            message="network unavailable",
            request=httpx.Request("GET", "https://api.browserbase.com/v1/projects"),
        ),
    ],
    ids=("http-500", "connection-error"),
)
def test_preflight_keeps_operational_failures_distinct_from_credentials(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    _prepare_preflight(monkeypatch, error)

    with pytest.raises(RuntimeError) as excinfo:
        BrowserbaseEnvironment.preflight()

    assert not isinstance(excinfo.value, BrowserbaseCredentialError)


def test_preflight_retrieves_configured_project_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _prepare_preflight(monkeypatch, None)

    BrowserbaseEnvironment.preflight()

    assert len(clients) == 1
    assert clients[0].kwargs == {"api_key": "api-key"}
    assert clients[0].projects.retrieve_calls == ["project-id"]
    assert clients[0].entered is True
    assert clients[0].exited is True
    assert clients[0].closed is True
