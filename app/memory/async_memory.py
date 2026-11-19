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

"""Asynchronous background memory ingestion with PII stripping."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from copy import deepcopy
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.sessions.session import Session
from google.genai import types

from app.core.config import get_settings
from app.observability.logging import log_intent_and_outcome
from app.security.pii_redactor import redact_payload, redact_text

logger = logging.getLogger("skycare.memory")

_ALLOWED_MEMORY_STATE_KEYS = frozenset(
    {
        "active_pnr",
        "active_origin",
        "active_destination",
        "active_cabin",
        "active_loyalty_tier",
        "recommended_itinerary_id",
        "traveler_preferences",
    }
)


def sanitize_session_for_memory(session: Session) -> Session:
    """Produce a shallow-cloned Session with PII masked from events and state."""
    safe_events = []
    for event in session.events:
        cloned = event.model_copy(deep=True)
        if cloned.content and cloned.content.parts:
            redacted_parts: list[types.Part] = []
            for part in cloned.content.parts:
                text = getattr(part, "text", None)
                if text:
                    redacted_parts.append(
                        types.Part.from_text(text=redact_text(text).text)
                    )
                else:
                    redacted_parts.append(part)
            cloned.content = types.Content(
                role=cloned.content.role, parts=redacted_parts
            )
        safe_events.append(cloned)

    safe_state: dict[str, Any] = {}
    for key, val in session.state.items():
        if key in _ALLOWED_MEMORY_STATE_KEYS:
            safe_state[key] = redact_payload(deepcopy(val))

    return Session(
        id=session.id,
        app_name=session.app_name,
        user_id=session.user_id,
        state=safe_state,
        events=safe_events,
        last_update_time=session.last_update_time,
    )


class AsyncMemoryCoordinator:
    """Tracks non-blocking background memory consolidation tasks."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self.scheduled_count: int = 0
        self.completed_count: int = 0
        self.failed_count: int = 0

    @property
    def pending_count(self) -> int:
        """Number of background memory tasks currently running."""
        return len(self._tasks)

    async def _ingest_task(
        self,
        *,
        memory_service: Any,
        sanitized_session: Session,
        actor: str,
        invocation_id: str | None,
    ) -> None:
        started = time.perf_counter()
        try:
            await memory_service.add_session_to_memory(sanitized_session)
            self.completed_count += 1
            log_intent_and_outcome(
                event_type="async_memory",
                actor=actor,
                session_id=sanitized_session.id,
                invocation_id=invocation_id,
                intent="Persist PII-redacted traveler preferences to long-term memory",
                intended_action="add_session_to_memory_async",
                outcome="succeeded",
                outcome_detail=f"events_indexed={len(sanitized_session.events)}",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:  # pragma: no cover - defensive background handler
            self.failed_count += 1
            log_intent_and_outcome(
                event_type="async_memory",
                actor=actor,
                session_id=sanitized_session.id,
                invocation_id=invocation_id,
                intent="Persist PII-redacted traveler preferences to long-term memory",
                intended_action="add_session_to_memory_async",
                outcome="error",
                outcome_detail=str(exc),
                latency_ms=(time.perf_counter() - started) * 1000,
                level=logging.WARNING,
            )

    def schedule(self, callback_context: CallbackContext) -> asyncio.Task[None] | None:
        """Schedule memory extraction cleanly in background without blocking chat."""
        inv_ctx = getattr(callback_context, "_invocation_context", None)
        memory_service = getattr(inv_ctx, "memory_service", None) if inv_ctx else None
        session = getattr(inv_ctx, "session", None) if inv_ctx else None
        if memory_service is None or session is None or not session.events:
            return None

        settings = get_settings()
        sanitized = (
            sanitize_session_for_memory(session)
            if settings.memory_redaction_enabled
            else session
        )
        task = asyncio.create_task(
            self._ingest_task(
                memory_service=memory_service,
                sanitized_session=sanitized,
                actor=callback_context.agent_name,
                invocation_id=callback_context.invocation_id,
            ),
            name=f"skycare-memory-{session.id}",
        )
        self.scheduled_count += 1
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def flush(self, timeout: float = 5.0) -> None:
        """Await pending background memory writes during graceful shutdown or tests."""
        if not self._tasks:
            return
        pending = list(self._tasks)
        await asyncio.wait(pending, timeout=timeout)


@functools.cache
def get_memory_coordinator() -> AsyncMemoryCoordinator:
    """Process-wide async memory task coordinator."""
    return AsyncMemoryCoordinator()


async def after_agent_memory_callback(
    callback_context: CallbackContext,
) -> types.Content | None:
    """Non-blocking ADK callback scheduling memory bank consolidation for root turns."""
    settings = get_settings()
    if callback_context.agent_name != settings.root_agent_name:
        return None
    get_memory_coordinator().schedule(callback_context)
    return None
