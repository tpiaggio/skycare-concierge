# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""FastAPI entrypoint serving ADK, A2A, SkyCare Web UI, and HITL/Audit APIs."""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from pydantic import BaseModel, Field

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.core.config import get_settings
from app.memory.async_memory import get_memory_coordinator
from app.observability.logging import (
    configure_structured_logging,
    get_audit_events,
    log_intent_and_outcome,
)
from app.storage.repository import get_repository

load_dotenv()
configure_structured_logging()

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)


def _resolve_otel_to_cloud() -> bool:
    if explicit := os.environ.get("OTEL_TO_CLOUD"):
        return explicit.strip().lower() in ("1", "true", "yes")
    settings = get_settings()
    return not (settings.dry_run or settings.use_in_memory_session)


otel_to_cloud = _resolve_otel_to_cloud()

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = Path(__file__).resolve().parent / "web"


class ApprovalDecisionRequest(BaseModel):
    """Human-in-the-loop supervisor decision payload."""

    approved: bool
    reviewer: str = Field(default="ops_supervisor", min_length=2, max_length=64)
    decision_reason: str = Field(default="", max_length=300)


@contextlib.asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    """Initialize shared Runner, A2A endpoints, and flush async memory on shutdown."""
    from app.agent import app as adk_app
    from app.agent import root_agent

    # Warm operational SQLite/GCS repository cleanly on startup
    get_repository()

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        memory_service=services.get_memory_service(),
        auto_create_session=True,
    )
    fastapi_app.state.runner = runner
    fastapi_app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        fastapi_app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    try:
        yield
    finally:
        await get_memory_coordinator().flush(timeout=5.0)


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    memory_service_uri=services.MEMORY_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=otel_to_cloud,
    lifespan=lifespan,
)
app.title = "skycare-concierge"
app.description = (
    "Enterprise AI Airline Disruption & Passenger Recovery Desk "
    "(ADK Multi-Agent + A2A + HITL + Memory Bank)"
)

# Serve custom SkyCare Web UI at '/' and '/ui' while preserving ADK UI at '/dev-ui/'
if WEB_DIR.is_dir():
    app.router.routes = [
        route for route in app.router.routes if getattr(route, "path", None) != "/"
    ]
    app.mount(
        "/skycare-assets",
        StaticFiles(directory=str(WEB_DIR)),
        name="skycare-assets",
    )

    @app.get("/", include_in_schema=False)
    @app.get("/ui", include_in_schema=False)
    def serve_skycare_ui() -> FileResponse:
        """Serve the SkyCare Operations & Passenger Recovery Web UI."""
        return FileResponse(WEB_DIR / "index.html")


@app.get("/api/status")
def runtime_status() -> dict[str, Any]:
    """Return runtime model routing, HITL bounds, and async memory metrics."""
    settings = get_settings()
    coordinator = get_memory_coordinator()
    return {
        "status": "ok",
        "service": settings.service_name,
        "app_name": settings.app_name,
        "root_agent_name": settings.root_agent_name,
        "flash_model": settings.flash_model,
        "pro_model": settings.pro_model,
        "voucher_auto_approve_limit_usd": settings.voucher_auto_approve_limit_usd,
        "otel_to_cloud": otel_to_cloud,
        "memory_tasks": {
            "pending": coordinator.pending_count,
            "scheduled": coordinator.scheduled_count,
            "completed": coordinator.completed_count,
            "failed": coordinator.failed_count,
        },
    }


@app.get("/api/disruptions")
def list_disruptions() -> list[dict[str, Any]]:
    """Return active airline PNRs, disruption summaries, vouchers, and rebookings."""
    return get_repository().list_disruptions()


@app.get("/api/approvals")
def list_approvals(
    status: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List pending and historical supervisor HITL approval requests."""
    records = get_repository().list_approvals(status=status)
    return [record.model_dump() for record in records]


@app.post("/api/approvals/{approval_id}/decision")
def decide_approval(
    approval_id: str,
    decision: ApprovalDecisionRequest,
) -> dict[str, Any]:
    """Approve or reject a pending HITL supervisor authorization request."""
    repo = get_repository()
    existing = repo.get_approval_request(approval_id)
    if existing is None:
        raise HTTPException(
            status_code=404, detail=f"Approval '{approval_id}' not found."
        )
    if existing.status != "PENDING":
        raise HTTPException(
            status_code=409,
            detail=f"Approval '{approval_id}' is already '{existing.status}'.",
        )
    updated = repo.resolve_approval_request(
        approval_id,
        approved=decision.approved,
        reviewer=decision.reviewer,
        decision_reason=decision.decision_reason,
    )
    if updated is None:
        raise HTTPException(
            status_code=409, detail="Approval state changed concurrently."
        )

    log_intent_and_outcome(
        event_type="hitl_decision",
        actor=decision.reviewer,
        intent=f"Resolve supervisor HITL gate for {existing.action_type}",
        intended_action=f"approval:{approval_id}",
        outcome="approved" if decision.approved else "rejected",
        outcome_detail=decision.decision_reason or updated.summary,
        level=logging.INFO if decision.approved else logging.WARNING,
        metadata={"pnr": updated.pnr, "action_type": updated.action_type},
    )
    return updated.model_dump()


@app.get("/api/audit")
def list_audit_events(
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Return recent PII-redacted intent-vs-outcome structured audit logs."""
    return get_audit_events(limit=limit)


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
