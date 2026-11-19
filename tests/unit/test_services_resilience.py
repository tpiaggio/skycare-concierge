# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for Agent Engine session/memory runtime self-healing."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from google.adk.events.event import Event
from google.adk.memory import InMemoryMemoryService
from google.adk.memory.base_memory_service import (
    BaseMemoryService,
    SearchMemoryResponse,
)
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.sessions.session import Session

from app.app_utils import services


class _MissingRuntimeError(RuntimeError):
    """Simulated Vertex AI 404 NOT_FOUND: The ReasoningEngine does not exist."""


class _MissingRuntimeSessionDelegate(BaseSessionService):
    """Stub delegate that fails with ReasoningEngine 404 to test recovery."""

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> Session:
        raise _MissingRuntimeError(
            "404 NOT_FOUND. {'error': {'code': 404, 'message': 'The ReasoningEngine does not exist.', 'status': 'NOT_FOUND'}}"
        )

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: GetSessionConfig | None = None,
    ) -> Session | None:
        return None

    async def list_sessions(
        self, *, app_name: str, user_id: str | None = None
    ) -> ListSessionsResponse:
        raise _MissingRuntimeError("404 NOT_FOUND: The ReasoningEngine does not exist.")

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        raise _MissingRuntimeError("404 NOT_FOUND: The ReasoningEngine does not exist.")

    async def append_event(self, session: Session, event: Event) -> Event:
        raise _MissingRuntimeError("404 NOT_FOUND: The ReasoningEngine does not exist.")


class _FailingValueErrorSessionDelegate(InMemorySessionService):
    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Session:
        raise ValueError("Invalid session_id format")


class _MissingRuntimeMemoryDelegate(BaseMemoryService):
    async def add_session_to_memory(self, session: Session) -> None:
        raise _MissingRuntimeError("404 NOT_FOUND: The ReasoningEngine does not exist.")

    async def search_memory(
        self, *, app_name: str, user_id: str, query: str
    ) -> SearchMemoryResponse:
        raise _MissingRuntimeError("404 NOT_FOUND: The ReasoningEngine does not exist.")


def test_is_missing_runtime_error_detects_direct_and_chained_errors() -> None:
    """Only missing ReasoningEngine 404 errors trigger runtime cache recovery."""
    direct = RuntimeError(
        "404 NOT_FOUND. {'error': {'message': 'The ReasoningEngine does not exist.'}}"
    )
    assert services._is_missing_runtime_error(direct) is True

    wrapped = RuntimeError("RetryError")
    wrapped.__cause__ = direct
    assert services._is_missing_runtime_error(wrapped) is True

    normal_session_404 = RuntimeError("Session sessions/123 not found.")
    assert services._is_missing_runtime_error(normal_session_404) is False


def test_resilient_session_service_refreshes_on_missing_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleted ReasoningEngine triggers cache clear, rebuild once, and retry."""
    cache_clears = 0

    def _clear() -> None:
        nonlocal cache_clears
        cache_clears += 1

    monkeypatch.setattr(services._get_or_create_agent_runtime, "cache_clear", _clear)

    built_services: list[BaseSessionService] = [
        _MissingRuntimeSessionDelegate(),
        InMemorySessionService(),
    ]
    build_calls = 0

    def _builder() -> BaseSessionService:
        nonlocal build_calls
        build_calls += 1
        return built_services[min(build_calls - 1, len(built_services) - 1)]

    svc = services._ResilientSessionService(
        label="session service",
        builder=_builder,
        fallback_factory=InMemorySessionService,
    )

    async def _run_concurrent() -> list[Session]:
        return await asyncio.gather(
            *(
                svc.create_session(app_name="app", user_id=f"user_{idx}")
                for idx in range(5)
            )
        )

    sessions = asyncio.run(_run_concurrent())
    assert len(sessions) == 5
    assert all(session.id for session in sessions)
    assert cache_clears == 1
    assert build_calls == 2


def test_resilient_session_service_falls_back_on_startup_failure() -> None:
    """Startup runtime error gracefully degrades to InMemorySessionService."""

    def _broken_builder() -> BaseSessionService:
        raise RuntimeError("Vertex control plane unreachable")

    svc = services._ResilientSessionService(
        label="session service",
        builder=_broken_builder,
        fallback_factory=InMemorySessionService,
    )
    created = asyncio.run(svc.create_session(app_name="app", user_id="elena_gold"))
    assert created.user_id == "elena_gold"
    listed = asyncio.run(svc.list_sessions(app_name="app", user_id="elena_gold"))
    assert len(listed.sessions) == 1


def test_resilient_session_service_propagates_validation_value_error() -> None:
    """Caller ValueError propagates directly without triggering fallback."""
    svc = services._ResilientSessionService(
        label="session service",
        builder=_FailingValueErrorSessionDelegate,
        fallback_factory=InMemorySessionService,
    )
    with pytest.raises(ValueError, match="Invalid session_id format"):
        asyncio.run(svc.create_session(app_name="app", user_id="elena_gold"))


def test_resilient_memory_service_recovers_on_missing_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory service refreshes deleted ReasoningEngine cache and succeeds."""
    cache_clears = 0

    def _clear() -> None:
        nonlocal cache_clears
        cache_clears += 1

    monkeypatch.setattr(services._get_or_create_agent_runtime, "cache_clear", _clear)

    delegates: list[BaseMemoryService] = [
        _MissingRuntimeMemoryDelegate(),
        InMemoryMemoryService(),
    ]
    builds = 0

    def _builder() -> BaseMemoryService:
        nonlocal builds
        builds += 1
        return delegates[min(builds - 1, len(delegates) - 1)]

    mem = services._ResilientMemoryService(
        label="memory service",
        builder=_builder,
        fallback_factory=InMemoryMemoryService,
    )
    resp = asyncio.run(
        mem.search_memory(app_name="app", user_id="elena_gold", query="window seat")
    )
    assert resp.memories == []
    assert cache_clears == 1
    assert builds == 2
