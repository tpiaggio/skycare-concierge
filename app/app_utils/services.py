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

"""Process-wide ADK session, memory, and artifact services shared by all surfaces."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Generic, TypeVar

import agentplatform
from agentplatform._genai.types.common import ReasoningEngineContextSpec
from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.adk.cli.service_registry import get_service_registry
from google.adk.cli.utils.service_factory import (
    create_memory_service_from_options,
    create_session_service_from_options,
)
from google.adk.events.event import Event
from google.adk.memory import InMemoryMemoryService
from google.adk.memory.base_memory_service import (
    BaseMemoryService,
    SearchMemoryResponse,
)
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.sessions.session import Session

from app.app_utils.memory_config import build_memory_bank_config

SESSION_SERVICE_URI = "shared://session"
ARTIFACT_SERVICE_URI = "shared://artifact"
MEMORY_SERVICE_URI = "shared://memory"

_AGENT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_logger = logging.getLogger("skycare.services")

_ServiceT = TypeVar("_ServiceT")
_ResultT = TypeVar("_ResultT")


def _use_in_memory() -> bool:
    return os.environ.get("USE_IN_MEMORY_SESSION", "").lower() in (
        "true",
        "1",
        "yes",
    ) or os.environ.get("INTEGRATION_TEST", "").lower() in ("true", "1", "yes")


def _is_missing_runtime_error(exc: BaseException) -> bool:
    """Return True when an exception indicates a deleted/missing ReasoningEngine."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        msg = str(cur).lower()
        if "does not exist" in msg and (
            "reasoningengine" in msg or "reasoning engine" in msg
        ):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class _ResilientServiceBase(Generic[_ServiceT]):
    """Auto-refreshes cached Vertex AI Agent Runtime handles if deleted/rotated."""

    def __init__(
        self,
        *,
        label: str,
        builder: Callable[[], _ServiceT],
        fallback_factory: Callable[[], _ServiceT],
    ) -> None:
        self._label = label
        self._builder = builder
        self._fallback_factory = fallback_factory
        self._sync_lock = threading.Lock()
        self._generation = 0
        try:
            self._delegate = builder()
        except Exception as exc:
            _logger.warning(
                "Falling back to in-memory %s at startup for region %s: %s",
                label,
                agent_runtime_location(),
                exc,
            )
            self._delegate = fallback_factory()

    def _refresh_delegate(self, expected_generation: int) -> None:
        """Clear cached Agent Runtime lookup once and rebuild the delegate."""
        with self._sync_lock:
            if self._generation != expected_generation:
                return
            _get_or_create_agent_runtime.cache_clear()
            try:
                self._delegate = self._builder()
            except Exception as exc:
                _logger.warning(
                    "Failed rebuilding Vertex %s (%s); falling back to in-memory.",
                    self._label,
                    exc,
                )
                self._delegate = self._fallback_factory()
            self._generation += 1

    def _switch_to_fallback(self) -> None:
        with self._sync_lock:
            self._delegate = self._fallback_factory()
            self._generation += 1

    async def _call(
        self,
        operation: Callable[[_ServiceT], Awaitable[_ResultT]],
        *,
        allow_fallback_on_error: bool = False,
    ) -> _ResultT:
        generation = self._generation
        try:
            return await operation(self._delegate)
        except ValueError:
            raise
        except Exception as exc:
            if not _is_missing_runtime_error(exc):
                if allow_fallback_on_error:
                    _logger.error(
                        "Vertex %s call failed (%s); falling back to in-memory.",
                        self._label,
                        exc,
                    )
                    self._switch_to_fallback()
                    return await operation(self._delegate)
                raise

            _logger.warning(
                "Vertex %s runtime missing (%s); refreshing Agent Runtime and retrying.",
                self._label,
                exc,
            )
            await asyncio.to_thread(self._refresh_delegate, generation)
            try:
                return await operation(self._delegate)
            except ValueError:
                raise
            except Exception as retry_exc:
                if allow_fallback_on_error:
                    _logger.error(
                        "Retry after refreshing %s runtime failed (%s); falling back to in-memory.",
                        self._label,
                        retry_exc,
                    )
                    self._switch_to_fallback()
                    return await operation(self._delegate)
                raise


class _ResilientSessionService(
    _ResilientServiceBase[BaseSessionService], BaseSessionService
):
    """Session service proxy that recovers transparently from deleted Agent Runtimes."""

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> Session:
        return await self._call(
            lambda svc: svc.create_session(
                app_name=app_name,
                user_id=user_id,
                state=state,
                session_id=session_id,
                **kwargs,
            ),
            allow_fallback_on_error=True,
        )

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: GetSessionConfig | None = None,
    ) -> Session | None:
        return await self._call(
            lambda svc: svc.get_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
                config=config,
            )
        )

    async def list_sessions(
        self, *, app_name: str, user_id: str | None = None
    ) -> ListSessionsResponse:
        return await self._call(
            lambda svc: svc.list_sessions(app_name=app_name, user_id=user_id)
        )

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        await self._call(
            lambda svc: svc.delete_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
            )
        )

    async def get_user_state(self, *, app_name: str, user_id: str) -> dict[str, Any]:
        return await self._call(
            lambda svc: svc.get_user_state(app_name=app_name, user_id=user_id)
        )

    async def append_event(self, session: Session, event: Event) -> Event:
        return await self._call(lambda svc: svc.append_event(session, event))

    async def flush(self) -> None:
        await self._delegate.flush()


class _ResilientMemoryService(
    _ResilientServiceBase[BaseMemoryService], BaseMemoryService
):
    """Memory service proxy that recovers transparently from deleted Agent Runtimes."""

    async def add_session_to_memory(self, session: Session) -> None:
        await self._call(
            lambda svc: svc.add_session_to_memory(session),
            allow_fallback_on_error=True,
        )

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        await self._call(
            lambda svc: svc.add_events_to_memory(
                app_name=app_name,
                user_id=user_id,
                events=events,
                session_id=session_id,
                custom_metadata=custom_metadata,
            ),
            allow_fallback_on_error=True,
        )

    async def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        await self._call(
            lambda svc: svc.add_memory(
                app_name=app_name,
                user_id=user_id,
                memories=memories,
                custom_metadata=custom_metadata,
            ),
            allow_fallback_on_error=True,
        )

    async def search_memory(
        self, *, app_name: str, user_id: str, query: str
    ) -> SearchMemoryResponse:
        return await self._call(
            lambda svc: svc.search_memory(
                app_name=app_name,
                user_id=user_id,
                query=query,
            ),
            allow_fallback_on_error=True,
        )

    async def delete_memory(
        self, *, app_name: str, user_id: str, memory_id: str | None = None
    ) -> None:
        await self._call(
            lambda svc: svc.delete_memory(
                app_name=app_name,
                user_id=user_id,
                memory_id=memory_id,
            )
        )


@functools.cache
def _get_or_create_agent_runtime():
    """Find or create the regional Agent Platform runtime for Sessions + Memory Bank."""
    default_agent_name = "skycare-concierge"
    agent_name = os.environ.get("AGENT_ENGINE_SESSION_NAME", default_agent_name)
    client = agentplatform.Client(location=agent_runtime_location())
    existing_agents = list(
        client.runtimes.list(config={"filter": f"display_name={agent_name}"})
    )
    if existing_agents:
        return existing_agents[0]
    context_spec = ReasoningEngineContextSpec(
        memory_bank_config=build_memory_bank_config(),
    )
    return client.runtimes.create(
        config={
            "display_name": agent_name,
            "context_spec": context_spec,
        }
    )


def _build_vertex_session_service() -> BaseSessionService:
    runtime = _get_or_create_agent_runtime()
    return create_session_service_from_options(
        base_dir=_AGENT_DIR,
        session_service_uri=f"agentengine://{runtime.api_resource.name}",
    )


def _build_vertex_memory_service() -> BaseMemoryService:
    runtime = _get_or_create_agent_runtime()
    return create_memory_service_from_options(
        base_dir=_AGENT_DIR,
        memory_service_uri=f"agentengine://{runtime.api_resource.name}",
    )


@functools.cache
def get_session_service() -> BaseSessionService:
    """Process-wide session service shared across every serving surface."""
    if not _use_in_memory():
        return _ResilientSessionService(
            label="session service",
            builder=_build_vertex_session_service,
            fallback_factory=InMemorySessionService,
        )
    return InMemorySessionService()


@functools.cache
def get_memory_service() -> BaseMemoryService:
    """Process-wide cross-session memory service shared by ADK runner & Web UI."""
    if not _use_in_memory():
        return _ResilientMemoryService(
            label="memory service",
            builder=_build_vertex_memory_service,
            fallback_factory=InMemoryMemoryService,
        )
    return InMemoryMemoryService()


def agent_runtime_location() -> str:
    """Region for the session Agent Runtime; it doesn't support 'global'."""
    if location := os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION"):
        return location
    location = os.environ.get("GOOGLE_CLOUD_LOCATION")
    if location and location != "global":
        return location
    return "us-east1"


@functools.cache
def get_artifact_service():
    """Process-wide artifact service: GCS when a bucket is set, else in-memory."""
    if bucket := os.environ.get("LOGS_BUCKET_NAME"):
        return GcsArtifactService(bucket_name=bucket)
    return InMemoryArtifactService()


_registry = get_service_registry()
_registry.register_session_service("shared", lambda uri, **kw: get_session_service())
_registry.register_artifact_service("shared", lambda uri, **kw: get_artifact_service())
_registry.register_memory_service("shared", lambda uri, **kw: get_memory_service())
