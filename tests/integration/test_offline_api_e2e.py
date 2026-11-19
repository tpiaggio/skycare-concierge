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

"""Offline FastAPI, Web UI, HITL approval REST API, and A2A endpoint tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.observability.logging import clear_audit_events, log_intent_and_outcome
from app.storage.repository import ReservationRepository, get_repository


def test_offline_fastapi_ui_hitl_and_a2a_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Web UI, disruptions API, HITL approvals flow, audit API, and A2A card."""
    monkeypatch.setenv("USE_IN_MEMORY_SESSION", "true")
    monkeypatch.setenv("SKYCARE_DRY_RUN", "true")
    monkeypatch.setenv("SKYCARE_DB_PATH", str(tmp_path / "api_test.db"))
    get_repository.cache_clear()
    clear_audit_events()

    repo = ReservationRepository(db_path=tmp_path / "api_test.db")
    monkeypatch.setattr("app.fast_api_app.get_repository", lambda: repo)

    from app.fast_api_app import app

    with TestClient(app) as client:
        ui = client.get("/")
        assert ui.status_code == 200
        assert "SkyCare Concierge" in ui.text

        asset = client.get("/skycare-assets/app.js")
        assert asset.status_code == 200

        status_resp = client.get("/api/status")
        assert status_resp.status_code == 200
        status_json = status_resp.json()
        assert status_json["flash_model"] == "gemini-3.8-flash"
        assert status_json["pro_model"] == "gemini-4-carbon"

        disruptions = client.get("/api/disruptions")
        assert disruptions.status_code == 200
        pnrs = {item["pnr"] for item in disruptions.json()}
        assert {"SKY842", "SKY319", "SKY905", "SKY404"} <= pnrs

        # Create pending HITL approval and resolve it through REST endpoint
        pending = repo.create_approval_request(
            pnr="SKY319",
            action_type="ISSUE_VOUCHER",
            summary="Overnight hotel voucher $210",
            payload={"pnr": "SKY319", "voucher_type": "HOTEL", "amount_usd": 210.0},
            estimated_cost_usd=210.0,
        )
        pending_list = client.get("/api/approvals?status=PENDING")
        assert pending_list.status_code == 200
        assert any(a["approval_id"] == pending.approval_id for a in pending_list.json())

        decision = client.post(
            f"/api/approvals/{pending.approval_id}/decision",
            json={
                "approved": True,
                "reviewer": "ops_lead",
                "decision_reason": "Approved mechanical delay hotel",
            },
        )
        assert decision.status_code == 200
        assert decision.json()["status"] == "APPROVED"

        # Resolving same approval twice must return 409 Conflict
        conflict = client.post(
            f"/api/approvals/{pending.approval_id}/decision",
            json={"approved": False, "reviewer": "ops_lead"},
        )
        assert conflict.status_code == 409

        # Audit endpoint must stay PII-redacted
        log_intent_and_outcome(
            event_type="tool_call",
            actor="ops_test",
            intent="Verify passenger email elena@example.com",
            intended_action="lookup",
            outcome="succeeded",
        )
        audit = client.get("/api/audit?limit=10")
        assert audit.status_code == 200
        serialized = str(audit.json())
        assert "elena@example.com" not in serialized

        card = client.get("/a2a/app/.well-known/agent-card.json")
        assert card.status_code == 200
        assert card.json()["name"] == "skycare_concierge"

        home = client.get("/")
        assert home.status_code == 200
        assert "langSelect" in home.text

        js = client.get("/skycare-assets/app.js")
        assert js.status_code == 200
        assert "Bandeja de Aprobación HITL" in js.text
        assert "Pro 4 (Carbon)" in js.text
        assert "Intl.NumberFormat" in js.text
        assert "Pro 3.1" not in js.text

        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["pro_model"] == "gemini-4-carbon"
