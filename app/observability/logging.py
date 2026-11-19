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

"""Structured JSON logging with OpenTelemetry trace correlation and PII masking."""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.security.pii_redactor import redact_payload, redact_text

_AUDIT_LOCK = threading.Lock()
_AUDIT_BUFFER: deque[AuditEvent] = deque(maxlen=250)


class AuditEvent(BaseModel):
    """PII-redacted record of an agent or tool intent and its observed outcome."""

    timestamp: str
    event_type: str
    actor: str
    model: str | None = None
    session_id: str | None = None
    invocation_id: str | None = None
    intent: str
    intended_action: str
    outcome: str
    outcome_detail: str = ""
    latency_ms: float | None = None
    trace_id: str | None = None
    span_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def current_trace_fields() -> dict[str, Any]:
    """Extract current OpenTelemetry trace and span IDs for Cloud Logging correlation."""
    span = trace.get_current_span()
    if span is None:
        return {}
    ctx = span.get_span_context()
    if ctx is None or not ctx.is_valid:
        return {}
    trace_hex = format(ctx.trace_id, "032x")
    span_hex = format(ctx.span_id, "016x")
    fields: dict[str, Any] = {
        "trace_id": trace_hex,
        "span_id": span_hex,
        "logging.googleapis.com/spanId": span_hex,
        "logging.googleapis.com/trace_sampled": bool(ctx.trace_flags.sampled),
    }
    project = get_settings().google_cloud_project
    if project:
        fields["logging.googleapis.com/trace"] = (
            f"projects/{project}/traces/{trace_hex}"
        )
    return fields


class StructuredJsonFormatter(logging.Formatter):
    """Formats every log record as single-line PII-redacted Cloud Logging JSON."""

    _RESERVED = frozenset(
        {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "message",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        settings = get_settings()
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "severity": record.levelname,
            "service": settings.service_name,
            "logger": record.name,
            "message": redact_text(record.getMessage()).text,
        }
        payload.update(current_trace_fields())

        extra_fields: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                extra_fields[key] = value
        if extra_fields:
            payload.update(redact_payload(extra_fields))

        if record.exc_info:
            payload["exception"] = redact_text(
                self.formatException(record.exc_info)
            ).text
        return json.dumps(payload, default=str, ensure_ascii=True)


def configure_structured_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the ``skycare`` logger hierarchy for PII-safe JSON stdout output."""
    logger = logging.getLogger("skycare")
    logger.setLevel(level)
    if not any(
        isinstance(getattr(h, "formatter", None), StructuredJsonFormatter)
        for h in logger.handlers
    ):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredJsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    return logger


def log_intent_and_outcome(
    *,
    event_type: str,
    actor: str,
    intent: str,
    intended_action: str,
    outcome: str,
    outcome_detail: str = "",
    model: str | None = None,
    session_id: str | None = None,
    invocation_id: str | None = None,
    latency_ms: float | None = None,
    metadata: dict[str, Any] | None = None,
    level: int = logging.INFO,
) -> AuditEvent:
    """Log both declared intent and actual outcome cleanly with PII redaction."""
    logger = configure_structured_logging()
    trace_info = current_trace_fields()
    clean_meta = redact_payload(metadata or {})
    event = AuditEvent(
        timestamp=datetime.now(UTC).isoformat(),
        event_type=event_type,
        actor=actor,
        model=model,
        session_id=session_id,
        invocation_id=invocation_id,
        intent=redact_text(intent).text,
        intended_action=redact_text(intended_action).text,
        outcome=outcome,
        outcome_detail=redact_text(outcome_detail).text,
        latency_ms=round(latency_ms, 2) if latency_ms is not None else None,
        trace_id=trace_info.get("trace_id"),
        span_id=trace_info.get("span_id"),
        metadata=clean_meta if isinstance(clean_meta, dict) else {},
    )
    with _AUDIT_LOCK:
        _AUDIT_BUFFER.append(event)

    logger.log(
        level,
        "%s action=%s outcome=%s",
        event_type,
        intended_action,
        outcome,
        extra={"audit": event.model_dump(exclude_none=True)},
    )
    return event


def get_audit_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent PII-redacted intent-vs-outcome audit records."""
    with _AUDIT_LOCK:
        items = list(_AUDIT_BUFFER)[-max(1, limit) :]
    return [item.model_dump() for item in reversed(items)]


def clear_audit_events() -> None:
    """Clear the in-memory ring buffer (intended for isolated test setup)."""
    with _AUDIT_LOCK:
        _AUDIT_BUFFER.clear()
