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

"""Unit tests for SQLite ReservationRepository, tools, schemas, and HITL gates."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.storage.repository import ReservationRepository, get_repository
from app.tools.airline_tools import (
    calculate_rebooking_options,
    check_eu261_dot_compensation_eligibility,
    execute_passenger_rebooking,
    issue_hotel_meal_voucher,
    lookup_passenger_reservation,
    search_alternate_itineraries,
)


@pytest.fixture
def isolated_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ReservationRepository:
    """Provide an isolated SQLite DB per test without touching workspace artifacts."""
    db_file = tmp_path / "skycare_test.db"
    monkeypatch.setenv("SKYCARE_DB_PATH", str(db_file))
    monkeypatch.setenv("SKYCARE_DRY_RUN", "true")
    monkeypatch.setenv("USE_IN_MEMORY_SESSION", "true")
    repo = ReservationRepository(db_path=db_file)
    monkeypatch.setattr("app.tools.airline_tools.get_repository", lambda: repo)
    monkeypatch.setattr("app.storage.repository.get_repository", lambda: repo)
    get_repository.cache_clear()
    return repo


def test_lookup_reservation_and_guided_error_recovery(
    isolated_repo: ReservationRepository,
) -> None:
    """Verify valid PNR lookup alongside actionable error recovery hints."""
    bad_fmt = lookup_passenger_reservation("bad")
    assert bad_fmt["status"] == "error"
    assert bad_fmt["error_code"] == "INVALID_PNR_FORMAT"
    assert "6-character" in bad_fmt["recovery_hint"]

    missing = lookup_passenger_reservation("ZZ9999")
    assert missing["status"] == "error"
    assert missing["error_code"] == "PNR_NOT_FOUND"
    assert "lookup_passenger_reservation" in missing["suggested_next_tools"]

    ok = lookup_passenger_reservation("sky842")
    assert ok["status"] == "ok"
    assert ok["pnr"] == "SKY842"
    assert ok["disrupted_flight"] == "SC402"
    assert ok["preferences"]["seat"] == "aisle"


def test_alternate_search_and_rebooking_scoring(
    isolated_repo: ReservationRepository,
) -> None:
    """Ensure invalid airport codes recover gracefully and SC408 nonstop ranks top."""
    bad_iata = search_alternate_itineraries("New York", "ZRH")
    assert bad_iata["status"] == "error"
    assert bad_iata["error_code"] == "INVALID_ITINERARY_SEARCH"

    search_res = search_alternate_itineraries("JFK", "ZRH", "Economy")
    assert search_res["status"] == "ok"
    assert search_res["options_count"] >= 3

    ranked = calculate_rebooking_options("SKY842", "Economy")
    assert ranked["status"] == "ok"
    assert ranked["recommended_itinerary_id"] == "SC408-NONSTOP"
    assert ranked["options"][0]["fare_waived"] is True
    assert ranked["options"][0]["passenger_net_cost_usd"] == 0.0


def test_regulatory_compensation_eligibility(
    isolated_repo: ReservationRepository,
) -> None:
    """Check UK/EU261, US DOT mechanical, and on-time negative control verdicts."""
    eu = check_eu261_dot_compensation_eligibility("SKY842")
    assert eu["regulation_framework"] == "UK_EU261"
    assert eu["eligible_for_cash_compensation"] is True
    assert eu["cash_compensation_eur"] == 600
    assert eu["waive_fare_difference"] is True

    dot = check_eu261_dot_compensation_eligibility("SKY319")
    assert dot["regulation_framework"] == "US_DOT"
    assert dot["eligible_for_duty_of_care"] is True
    assert dot["hotel_allowance_usd"] == 220.0

    ontime = check_eu261_dot_compensation_eligibility("SKY404")
    assert ontime["eligible_for_cash_compensation"] is False
    assert ontime["eligible_for_duty_of_care"] is False
    assert ontime["waive_fare_difference"] is False


def test_voucher_auto_approve_vs_hitl_gate_and_single_use(
    isolated_repo: ReservationRepository,
) -> None:
    """Auto-issue <= $150 meal vouchers; gate > $150 hotels and prevent replay."""
    meal = issue_hotel_meal_voucher("SKY905", "MEAL", 45.0, "Atlanta Concourse Cafe")
    assert meal["status"] == "issued"
    assert meal["voucher_id"].startswith("VCH-")

    # Ineligible passenger blocked deterministically
    ineligible = issue_hotel_meal_voucher("SKY404", "MEAL", 25.0, "Airport Bistro")
    assert ineligible["status"] == "error"
    assert ineligible["error_code"] == "NOT_ELIGIBLE_FOR_VOUCHER"

    # High-value hotel triggers HITL pending approval
    hotel_pending = issue_hotel_meal_voucher(
        "SKY319", "HOTEL", 210.0, "SFO Airport Hilton"
    )
    assert hotel_pending["status"] == "awaiting_confirmation"
    approval_id = hotel_pending["approval_id"]
    assert approval_id.startswith("APR-")

    # Supervisor approves
    isolated_repo.resolve_approval_request(
        approval_id,
        approved=True,
        reviewer="ops_supervisor",
        decision_reason="Mechanical delay",
    )
    hotel_issued = issue_hotel_meal_voucher(
        "SKY319", "HOTEL", 210.0, "SFO Airport Hilton", approval_id=approval_id
    )
    assert hotel_issued["status"] == "issued"
    assert hotel_issued["voucher_id"].startswith("VCH-")

    # Single-use token is now CONSUMED; replaying same approval_id cannot mint another voucher
    replay = issue_hotel_meal_voucher(
        "SKY319", "HOTEL", 210.0, "SFO Airport Hilton", approval_id=approval_id
    )
    assert replay["status"] == "awaiting_confirmation"
    assert replay["approval_id"] != approval_id


def test_rebooking_hitl_block_rejection_and_approval_execution(
    isolated_repo: ReservationRepository,
) -> None:
    """Verify PNR rebooking pauses without approval, honors rejection, and commits on sign-off."""
    before_flight = isolated_repo.get_flight("SC408")
    assert before_flight is not None
    initial_seats = before_flight.economy_seats

    # 1. Attempting to commit without approval must pause and NOT decrement seats
    pending = execute_passenger_rebooking(
        "SKY842", ["SC408"], cabin_class="Economy", seat_preference="AISLE"
    )
    assert pending["status"] == "awaiting_confirmation"
    apr_1 = pending["approval_id"]
    after_pending = isolated_repo.get_flight("SC408")
    assert after_pending is not None
    assert after_pending.economy_seats == initial_seats

    # 2. Supervisor rejection preserves seat inventory and surfaces rejection clearly
    isolated_repo.resolve_approval_request(
        apr_1, approved=False, reviewer="ops_supervisor", decision_reason="Hold seat"
    )
    rejected = execute_passenger_rebooking(
        "SKY842",
        ["SC408"],
        cabin_class="Economy",
        seat_preference="AISLE",
        approval_id=apr_1,
    )
    assert rejected["status"] == "rejected"
    after_reject = isolated_repo.get_flight("SC408")
    assert after_reject is not None
    assert after_reject.economy_seats == initial_seats

    # 3. Native ADK ToolConfirmation authorized path succeeds and updates seat + DB
    fake_ctx = SimpleNamespace(
        tool_confirmation=SimpleNamespace(confirmed=True),
        state={},
        agent_name="passenger_care_agent",
        _invocation_context=None,
    )
    confirmed = execute_passenger_rebooking(
        "SKY842",
        ["SC408"],
        cabin_class="Economy",
        seat_preference="AISLE",
        tool_context=fake_ctx,
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["rebooking_id"].startswith("RBK-")
    after_confirm = isolated_repo.get_flight("SC408")
    assert after_confirm is not None
    assert after_confirm.economy_seats == initial_seats - 1
