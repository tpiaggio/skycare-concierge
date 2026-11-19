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

"""Airline disruption recovery tools with strict schemas and guided recovery."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Literal

from google.adk.tools import FunctionTool, ToolContext
from pydantic import ValidationError

from app.core.config import get_settings
from app.observability.logging import log_intent_and_outcome
from app.security.secrets import SecretLookupError, get_secret_provider
from app.storage.repository import get_repository
from app.tools.schemas import (
    CompensationEligibilityOutput,
    FlightSearchOutput,
    FlightSegmentSchema,
    ItineraryCandidate,
    ItinerarySearchInput,
    LookupReservationInput,
    PassengerLookupOutput,
    RebookingExecutionInput,
    RebookingExecutionOutput,
    RebookingProposalOutput,
    ScoredRebookingOption,
    ToolErrorOutput,
    VoucherInput,
    VoucherIssuanceOutput,
)


def _error(
    *,
    error_code: str,
    message: str,
    recovery_hint: str,
    suggested_next_tools: list[str] | None = None,
) -> dict[str, Any]:
    return ToolErrorOutput(
        error_code=error_code,
        message=message,
        recovery_hint=recovery_hint,
        suggested_next_tools=suggested_next_tools or [],
    ).model_dump()


def _ctx_ids(tool_context: ToolContext | None) -> tuple[str | None, str | None, str]:
    if tool_context is None:
        return None, None, "skycare_tool"
    inv = getattr(tool_context, "_invocation_context", None)
    session_id = getattr(getattr(inv, "session", None), "id", None)
    invocation_id = getattr(inv, "invocation_id", None)
    actor = getattr(tool_context, "agent_name", None) or "skycare_tool"
    return session_id, invocation_id, actor


def _verify_gds_partner_connectivity_note() -> str:
    """Resolve optional partner key via SecretProvider without ever exposing plaintext."""
    try:
        secret = get_secret_provider().get_secret("gds-partner-api-key")
        # Only verify non-empty length internally; never return secret material.
        return "partner_gds_authenticated" if secret.reveal() else "local_gds_mock"
    except SecretLookupError:
        return "local_gds_sandbox"


def _requires_native_adk_confirmation(*_args: Any, **_kwargs: Any) -> bool:
    """Enable ADK FunctionTool native pause when SKYCARE_NATIVE_ADK_CONFIRMATION=true."""
    return os.environ.get("SKYCARE_NATIVE_ADK_CONFIRMATION", "").lower() in {
        "1",
        "true",
        "yes",
    }


def lookup_passenger_reservation(
    pnr: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Look up a passenger PNR, full flight itinerary, disruption status, and preferences.

    Purpose:
        Retrieves authoritative booking details, current segment statuses, loyalty
        tier, disruption root cause, and stored seating/meal preferences from the
        operational reservation database.

    Preconditions:
        `pnr` must be a 6-character alphanumeric SkyCare booking code (for example,
        `SKY842` or `SKY319`).

    Side Effects:
        Read-only operation. Also refreshes `active_pnr`, `active_origin`,
        `active_destination`, and `active_cabin` in persistent ADK session state
        when `tool_context` is provided, and emits a PII-redacted intent/outcome
        audit log entry.

    Args:
        pnr: 6-character alphanumeric Passenger Name Record (e.g., ``SKY842``).
        tool_context: Injected ADK tool context used for session state persistence.

    Returns:
        A validated ``PassengerLookupOutput`` dictionary on success, or a
        ``ToolErrorOutput`` dictionary containing ``error_code`` and
        ``recovery_hint`` if the PNR format is invalid or missing.
    """
    started = time.perf_counter()
    session_id, invocation_id, actor = _ctx_ids(tool_context)

    try:
        validated = LookupReservationInput(pnr=pnr)
    except ValidationError:
        payload = _error(
            error_code="INVALID_PNR_FORMAT",
            message=f"'{pnr}' is not a valid 6-character alphanumeric PNR.",
            recovery_hint=(
                "Ask the passenger to confirm their 6-character booking code "
                "(letters and digits only, e.g., SKY842), then call "
                "lookup_passenger_reservation again."
            ),
            suggested_next_tools=["lookup_passenger_reservation"],
        )
        log_intent_and_outcome(
            event_type="tool_call",
            actor=actor,
            session_id=session_id,
            invocation_id=invocation_id,
            intent="Validate and retrieve passenger PNR reservation",
            intended_action=f"lookup_passenger_reservation(pnr={pnr})",
            outcome="failed_validation",
            outcome_detail=payload["message"],
            latency_ms=(time.perf_counter() - started) * 1000,
            level=logging.WARNING,
        )
        return payload

    repo = get_repository()
    reservation, segments = repo.get_reservation_by_pnr(validated.pnr)
    if reservation is None:
        payload = _error(
            error_code="PNR_NOT_FOUND",
            message=f"No reservation found for PNR '{validated.pnr}'.",
            recovery_hint=(
                "Double-check the spelling of the 6-character PNR with the traveler "
                "or verify whether the booking was issued on an interline partner."
            ),
            suggested_next_tools=["lookup_passenger_reservation"],
        )
        log_intent_and_outcome(
            event_type="tool_call",
            actor=actor,
            session_id=session_id,
            invocation_id=invocation_id,
            intent="Retrieve passenger PNR reservation",
            intended_action=f"lookup_passenger_reservation(pnr={validated.pnr})",
            outcome="not_found",
            outcome_detail=payload["message"],
            latency_ms=(time.perf_counter() - started) * 1000,
            level=logging.WARNING,
        )
        return payload

    if tool_context is not None:
        tool_context.state["active_pnr"] = reservation.pnr
        tool_context.state["active_origin"] = reservation.origin
        tool_context.state["active_destination"] = reservation.destination
        tool_context.state["active_cabin"] = reservation.cabin_class
        tool_context.state["active_loyalty_tier"] = reservation.loyalty_tier

    result = PassengerLookupOutput(
        pnr=reservation.pnr,
        passenger_name=reservation.passenger_name,
        loyalty_tier=reservation.loyalty_tier,
        cabin_class=reservation.cabin_class,
        origin=reservation.origin,
        destination=reservation.destination,
        rebooking_status=reservation.rebooking_status,
        disrupted_flight=reservation.disrupted_flight,
        disruption_summary=reservation.disruption_summary,
        preferences=reservation.preferences,
        segments=[FlightSegmentSchema(**seg.model_dump()) for seg in segments],
    ).model_dump()

    log_intent_and_outcome(
        event_type="tool_call",
        actor=actor,
        session_id=session_id,
        invocation_id=invocation_id,
        intent="Retrieve passenger PNR reservation and update session context",
        intended_action=f"lookup_passenger_reservation(pnr={reservation.pnr})",
        outcome="succeeded",
        outcome_detail=(
            f"status={reservation.rebooking_status}, route={reservation.origin}->{reservation.destination}"
        ),
        latency_ms=(time.perf_counter() - started) * 1000,
        metadata={"pnr": reservation.pnr, "loyalty_tier": reservation.loyalty_tier},
    )
    return result


def search_alternate_itineraries(
    origin: str,
    destination: str,
    cabin_class: Literal["Economy", "Business"] = "Economy",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Search available nonstop and one-stop alternate flights with live seat inventory.

    Purpose:
        Finds scheduled ``ON_TIME`` single-leg and connecting itineraries between
        two airports that still have sellable inventory in the requested cabin.

    Preconditions:
        `origin` and `destination` must be 3-letter IATA airport codes (e.g.,
        ``JFK``, ``ZRH``, ``NRT``). Call ``lookup_passenger_reservation`` first if
        the traveler's airports are unknown.

    Side Effects:
        Read-only inventory query. Emits an intent/outcome audit log entry.

    Args:
        origin: 3-letter IATA origin airport code (e.g., ``JFK``).
        destination: 3-letter IATA destination airport code (e.g., ``ZRH``).
        cabin_class: Cabin class to filter seat inventory (``Economy`` or ``Business``).
        tool_context: Injected ADK tool context.

    Returns:
        A validated ``FlightSearchOutput`` dictionary listing matching candidates,
        or a ``ToolErrorOutput`` dictionary with recovery instructions.
    """
    started = time.perf_counter()
    session_id, invocation_id, actor = _ctx_ids(tool_context)

    try:
        validated = ItinerarySearchInput(
            origin=origin,
            destination=destination,
            cabin_class=cabin_class,
        )
    except ValidationError as exc:
        payload = _error(
            error_code="INVALID_ITINERARY_SEARCH",
            message=str(exc),
            recovery_hint=(
                "Ensure both origin and destination are 3-letter IATA codes "
                "(e.g., JFK, LHR, ZRH, ORD, NRT) and cabin_class is 'Economy' or 'Business'."
            ),
            suggested_next_tools=["search_alternate_itineraries"],
        )
        log_intent_and_outcome(
            event_type="tool_call",
            actor=actor,
            session_id=session_id,
            invocation_id=invocation_id,
            intent="Search alternate flights",
            intended_action=f"search_alternate_itineraries({origin}->{destination}, {cabin_class})",
            outcome="failed_validation",
            outcome_detail=payload["message"],
            latency_ms=(time.perf_counter() - started) * 1000,
            level=logging.WARNING,
        )
        return payload

    if validated.origin == validated.destination:
        return _error(
            error_code="IDENTICAL_AIRPORTS",
            message="Origin and destination airport codes cannot be identical.",
            recovery_hint="Verify the passenger's final destination code and retry.",
            suggested_next_tools=["lookup_passenger_reservation"],
        )

    gds_mode = _verify_gds_partner_connectivity_note()
    raw_options = get_repository().search_alternate_itineraries(
        validated.origin, validated.destination, validated.cabin_class
    )
    if not raw_options:
        payload = _error(
            error_code="NO_ALTERNATE_FLIGHTS",
            message=(
                f"No available {validated.cabin_class} alternate itineraries found "
                f"from {validated.origin} to {validated.destination}."
            ),
            recovery_hint=(
                "Try searching in the alternate cabin ('Economy' vs 'Business') or "
                "verify whether a nearby co-terminal airport is acceptable."
            ),
            suggested_next_tools=["search_alternate_itineraries"],
        )
        log_intent_and_outcome(
            event_type="tool_call",
            actor=actor,
            session_id=session_id,
            invocation_id=invocation_id,
            intent="Search alternate flights",
            intended_action=(
                f"search_alternate_itineraries({validated.origin}->{validated.destination})"
            ),
            outcome="empty_results",
            outcome_detail=payload["message"],
            latency_ms=(time.perf_counter() - started) * 1000,
            metadata={"gds_mode": gds_mode},
        )
        return payload

    output = FlightSearchOutput(
        origin=validated.origin,
        destination=validated.destination,
        cabin_class=validated.cabin_class,
        options_count=len(raw_options),
        itineraries=[ItineraryCandidate(**opt) for opt in raw_options],
    ).model_dump()

    log_intent_and_outcome(
        event_type="tool_call",
        actor=actor,
        session_id=session_id,
        invocation_id=invocation_id,
        intent="Search alternate flights with seat inventory",
        intended_action=(
            f"search_alternate_itineraries({validated.origin}->{validated.destination}, {validated.cabin_class})"
        ),
        outcome="succeeded",
        outcome_detail=f"options_found={len(raw_options)}",
        latency_ms=(time.perf_counter() - started) * 1000,
        metadata={"gds_mode": gds_mode},
    )
    return output


def check_eu261_dot_compensation_eligibility(
    pnr: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Evaluate deterministic EU261 / UK261 / US DOT compensation and duty-of-care rules.

    Purpose:
        Applies airline disruption policy regulations based on route distance,
        jurisdiction, delay duration, and controllable vs. extraordinary cause to
        determine cash compensation, fare-difference waiver eligibility, and
        hotel/meal voucher caps.

    Preconditions:
        `pnr` must belong to an existing passenger reservation.

    Side Effects:
        Read-only policy evaluation; updates ``last_compensation_verdict`` in
        session state when `tool_context` is available and logs intent/outcome.

    Args:
        pnr: 6-character Passenger Name Record (e.g., ``SKY842``).
        tool_context: Injected ADK tool context.

    Returns:
        A validated ``CompensationEligibilityOutput`` dictionary detailing amounts,
        waivers, and rationale, or a ``ToolErrorOutput`` dictionary.
    """
    started = time.perf_counter()
    session_id, invocation_id, actor = _ctx_ids(tool_context)

    try:
        validated = LookupReservationInput(pnr=pnr)
    except ValidationError:
        return _error(
            error_code="INVALID_PNR_FORMAT",
            message=f"'{pnr}' is not a valid 6-character PNR.",
            recovery_hint="Pass a 6-character alphanumeric PNR (e.g., SKY842).",
            suggested_next_tools=["lookup_passenger_reservation"],
        )

    repo = get_repository()
    reservation, segments = repo.get_reservation_by_pnr(validated.pnr)
    if reservation is None:
        return _error(
            error_code="PNR_NOT_FOUND",
            message=f"Reservation '{validated.pnr}' was not found.",
            recovery_hint="Verify the PNR with lookup_passenger_reservation first.",
            suggested_next_tools=["lookup_passenger_reservation"],
        )

    disrupted = next(
        (s for s in segments if s.flight_number == reservation.disrupted_flight),
        None,
    )
    if disrupted is None or (
        disrupted.status == "ON_TIME" and disrupted.delay_minutes == 0
    ):
        output = CompensationEligibilityOutput(
            pnr=reservation.pnr,
            regulation_framework="US_DOT",
            eligible_for_cash_compensation=False,
            cash_compensation_eur=0,
            eligible_for_duty_of_care=False,
            hotel_allowance_usd=0.0,
            meal_allowance_usd=0.0,
            waive_fare_difference=False,
            rationale=(
                "No qualifying airline disruption found; flight is scheduled ON_TIME. "
                "Voluntary changes remain subject to standard fare difference rules."
            ),
        ).model_dump()
    elif disrupted.jurisdiction in {"EU261", "UK_EU261"}:
        # Under EU/UK261, duty of care + fare-free rerouting always apply for long-haul
        # cancellations, while statutory EUR 600 applies unless purely extraordinary ATC.
        is_controllable = disrupted.disruption_cause in {
            "MECHANICAL_MAINTENANCE",
            "CREW_TIMEOUT_WEATHER",
        }
        cash_eur = 600 if (disrupted.distance_km >= 3500 and is_controllable) else 0
        output = CompensationEligibilityOutput(
            pnr=reservation.pnr,
            regulation_framework="UK_EU261",
            eligible_for_cash_compensation=cash_eur > 0,
            cash_compensation_eur=cash_eur,
            eligible_for_duty_of_care=True,
            hotel_allowance_usd=250.0,
            meal_allowance_usd=60.0,
            waive_fare_difference=True,
            rationale=(
                f"{disrupted.flight_number} ({disrupted.status}, cause={disrupted.disruption_cause}, "
                f"distance={disrupted.distance_km}km) triggers EU/UK261 Article 8 rerouting "
                "(fare difference waived) and Article 9 duty of care (meals + hotel up to $250/night). "
                + (
                    "Crew duty timeout contributes to statutory EUR 600 compensation."
                    if cash_eur > 0
                    else "Extraordinary weather/ATC excludes cash payout."
                )
            ),
        ).model_dump()
    else:
        # US DOT controllable vs weather/ATC matrix
        controllable = disrupted.disruption_cause == "MECHANICAL_MAINTENANCE"
        is_long_delay = (
            disrupted.delay_minutes >= 180 or disrupted.status == "CANCELLED"
        )
        hotel_cap = 220.0 if (controllable and is_long_delay) else 0.0
        meal_cap = (
            45.0
            if (disrupted.delay_minutes >= 60 or disrupted.status == "CANCELLED")
            else 0.0
        )
        output = CompensationEligibilityOutput(
            pnr=reservation.pnr,
            regulation_framework="US_DOT",
            eligible_for_cash_compensation=False,
            cash_compensation_eur=0,
            eligible_for_duty_of_care=hotel_cap > 0 or meal_cap > 0,
            hotel_allowance_usd=hotel_cap,
            meal_allowance_usd=meal_cap,
            waive_fare_difference=True,
            rationale=(
                f"{disrupted.flight_number} ({disrupted.status} {disrupted.delay_minutes}m, "
                f"cause={disrupted.disruption_cause}) falls under US DOT customer service "
                "commitments: irregular-operations rebooking fee/fare is waived on SkyCare "
                "itineraries; controllable mechanical misconnects authorize overnight hotel "
                "up to $220 (requires supervisor approval above $150) and meals up to $45."
            ),
        ).model_dump()

    if tool_context is not None:
        tool_context.state["last_compensation_verdict"] = output

    log_intent_and_outcome(
        event_type="tool_call",
        actor=actor,
        session_id=session_id,
        invocation_id=invocation_id,
        intent="Evaluate passenger regulatory compensation and duty-of-care rules",
        intended_action=f"check_eu261_dot_compensation_eligibility(pnr={validated.pnr})",
        outcome="succeeded",
        outcome_detail=(
            f"framework={output['regulation_framework']}, "
            f"cash_eur={output['cash_compensation_eur']}, "
            f"waive_fare={output['waive_fare_difference']}"
        ),
        latency_ms=(time.perf_counter() - started) * 1000,
    )
    return output


def calculate_rebooking_options(
    pnr: str,
    cabin_class: Literal["Economy", "Business"] = "Economy",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Score and rank alternate itineraries tailored to a disrupted passenger PNR.

    Purpose:
        Combines alternate flight search results with the passenger's loyalty
        tier, disruption waiver eligibility, connection risk, and travel
        preferences (such as avoiding tight connections or prioritizing direct
        flights) to produce a deterministic ranked recommendation list.

    Preconditions:
        `pnr` must correspond to a valid reservation. Recommended prior to asking
        for confirmation on `execute_passenger_rebooking`.

    Side Effects:
        Read-only scoring; stores ``recommended_options`` summary in session state.

    Args:
        pnr: 6-character Passenger Name Record (e.g., ``SKY842``).
        cabin_class: Target cabin class (``Economy`` or ``Business``).
        tool_context: Injected ADK tool context.

    Returns:
        A ``RebookingProposalOutput`` dictionary sorted from highest recovery score
        to lowest, or a ``ToolErrorOutput`` dictionary.
    """
    started = time.perf_counter()
    session_id, invocation_id, actor = _ctx_ids(tool_context)

    try:
        validated_pnr = LookupReservationInput(pnr=pnr).pnr
    except ValidationError:
        return _error(
            error_code="INVALID_PNR_FORMAT",
            message=f"'{pnr}' is not a valid 6-character PNR.",
            recovery_hint="Confirm the 6-character alphanumeric PNR and retry.",
            suggested_next_tools=["lookup_passenger_reservation"],
        )

    repo = get_repository()
    reservation, _ = repo.get_reservation_by_pnr(validated_pnr)
    if reservation is None:
        return _error(
            error_code="PNR_NOT_FOUND",
            message=f"Reservation '{validated_pnr}' not found.",
            recovery_hint="Verify the passenger PNR using lookup_passenger_reservation.",
            suggested_next_tools=["lookup_passenger_reservation"],
        )

    candidates = repo.search_alternate_itineraries(
        reservation.origin, reservation.destination, cabin_class
    )
    if not candidates:
        return _error(
            error_code="NO_RECOVERY_OPTIONS",
            message=(
                f"No available {cabin_class} alternates found on "
                f"{reservation.origin}->{reservation.destination}."
            ),
            recovery_hint="Retry with 'Economy' cabin_class or notify operations desk.",
            suggested_next_tools=["search_alternate_itineraries"],
        )

    comp = check_eu261_dot_compensation_eligibility(validated_pnr)
    fare_waived = bool(comp.get("waive_fare_difference", False))

    scored: list[ScoredRebookingOption] = []
    # Sort lexicographically by arrival time first so earlier arrivals get rank boost
    by_arrival = sorted(candidates, key=lambda c: c["arrival_utc"])
    arrival_rank = {c["itinerary_id"]: idx for idx, c in enumerate(by_arrival)}

    avoid_tight = bool(reservation.preferences.get("avoid_tight_connections", False))
    for opt in candidates:
        score = 88.0
        stops = int(opt["stops"])
        rank = arrival_rank[opt["itinerary_id"]]
        score -= stops * (14.0 if avoid_tight else 8.0)
        score -= rank * 6.0
        if reservation.loyalty_tier in {"Gold", "Platinum"} and stops == 0:
            score += 9.0
        if int(opt["available_seats"]) <= 2:
            score -= 3.0
        score = round(max(1.0, min(99.5, score)), 1)

        notes: list[str] = []
        notes.append(
            "Nonstop flight eliminates misconnect risk"
            if stops == 0
            else "1-stop connecting option"
        )
        notes.append(
            "Fare difference waived due to involuntary disruption"
            if fare_waived
            else f"Voluntary change fare diff ${opt['fare_difference_usd']:.2f}"
        )
        scored.append(
            ScoredRebookingOption(
                itinerary_id=opt["itinerary_id"],
                flight_numbers=[leg["flight_number"] for leg in opt["legs"]],
                stops=stops,
                arrival_utc=opt["arrival_utc"],
                passenger_net_cost_usd=0.0
                if fare_waived
                else float(opt["fare_difference_usd"]),
                fare_difference_usd=float(opt["fare_difference_usd"]),
                fare_waived=fare_waived,
                recovery_score=score,
                recommendation_notes="; ".join(notes),
            )
        )

    scored.sort(key=lambda item: item.recovery_score, reverse=True)
    result = RebookingProposalOutput(
        pnr=reservation.pnr,
        loyalty_tier=reservation.loyalty_tier,
        recommended_itinerary_id=scored[0].itinerary_id,
        options=scored,
    ).model_dump()

    if tool_context is not None:
        tool_context.state["recommended_itinerary_id"] = scored[0].itinerary_id
        tool_context.state["recommended_flights"] = scored[0].flight_numbers

    log_intent_and_outcome(
        event_type="tool_call",
        actor=actor,
        session_id=session_id,
        invocation_id=invocation_id,
        intent="Rank candidate recovery itineraries",
        intended_action=f"calculate_rebooking_options(pnr={reservation.pnr}, cabin={cabin_class})",
        outcome="succeeded",
        outcome_detail=f"recommended={scored[0].itinerary_id}, score={scored[0].recovery_score}",
        latency_ms=(time.perf_counter() - started) * 1000,
    )
    return result


def issue_hotel_meal_voucher(
    pnr: str,
    voucher_type: Literal["HOTEL", "MEAL", "GROUND_TRANSPORT"],
    amount_usd: float,
    vendor_name: str,
    approval_id: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Issue a duty-of-care meal, hotel, or ground transport voucher with HITL policy limits.

    Purpose:
        Creates an auditable duty-of-care voucher for a disrupted passenger. Amounts
        up to ``$150.00`` auto-issue within policy if the reservation is eligible for
        duty of care; amounts exceeding ``$150.00`` require Human-in-the-Loop
        supervisor approval before funds are committed.

    Preconditions:
        Call ``check_eu261_dot_compensation_eligibility`` first to confirm duty-of-care
        eligibility and regulatory allowance limits. Amounts must be between
        ``$0.01`` and ``$500.00``.

    Side Effects:
        Writes either an ``ISSUED`` voucher record OR creates a ``PENDING`` supervisor
        ``ApprovalRecord`` in the operational database when exceeding ``$150.00``.

    Args:
        pnr: 6-character Passenger Name Record (e.g., ``SKY319``).
        voucher_type: One of ``HOTEL``, ``MEAL``, or ``GROUND_TRANSPORT``.
        amount_usd: Voucher value in USD (positive, <= 500.00).
        vendor_name: Approved hospitality, restaurant, or transit vendor name.
        approval_id: Optional previously issued supervisor ``approval_id`` (``APR-...``).
        tool_context: Injected ADK tool context (checked for ``tool_confirmation``).

    Returns:
        A ``VoucherIssuanceOutput`` dictionary with status ``issued``,
        ``awaiting_confirmation``, or ``rejected`` (or ``ToolErrorOutput`` on
        validation failure).
    """
    started = time.perf_counter()
    session_id, invocation_id, actor = _ctx_ids(tool_context)
    settings = get_settings()

    try:
        validated = VoucherInput(
            pnr=pnr,
            voucher_type=voucher_type,
            amount_usd=amount_usd,
            vendor_name=vendor_name,
        )
    except ValidationError as exc:
        return _error(
            error_code="INVALID_VOUCHER_REQUEST",
            message=str(exc),
            recovery_hint=(
                "Verify PNR (6 chars), voucher_type ('HOTEL'|'MEAL'|'GROUND_TRANSPORT'), "
                "amount_usd (>0 and <=500), and vendor_name, then retry."
            ),
            suggested_next_tools=["check_eu261_dot_compensation_eligibility"],
        )

    repo = get_repository()
    reservation, _ = repo.get_reservation_by_pnr(validated.pnr)
    if reservation is None:
        return _error(
            error_code="PNR_NOT_FOUND",
            message=f"Reservation '{validated.pnr}' not found.",
            recovery_hint="Verify PNR via lookup_passenger_reservation first.",
            suggested_next_tools=["lookup_passenger_reservation"],
        )

    comp = check_eu261_dot_compensation_eligibility(validated.pnr)
    if not comp.get("eligible_for_duty_of_care", False):
        return _error(
            error_code="NOT_ELIGIBLE_FOR_VOUCHER",
            message=(
                f"PNR '{validated.pnr}' is not eligible for involuntary disruption vouchers: "
                f"{comp.get('rationale', '')}"
            ),
            recovery_hint=(
                "Inform the passenger politely that the flight is on schedule or outside "
                "duty-of-care criteria, and do not issue a voucher."
            ),
        )

    requires_supervisor = validated.amount_usd > settings.voucher_auto_approve_limit_usd
    consumed_approval_id: str | None = None
    if requires_supervisor:
        # Check ADK native ToolConfirmation first
        native_conf = (
            getattr(tool_context, "tool_confirmation", None)
            if tool_context is not None
            else None
        )
        if native_conf is not None:
            if not native_conf.confirmed:
                res = VoucherIssuanceOutput(
                    status="rejected",
                    pnr=validated.pnr,
                    voucher_type=validated.voucher_type,
                    amount_usd=validated.amount_usd,
                    vendor_name=validated.vendor_name,
                    requires_supervisor_approval=True,
                    message="Voucher request rejected via HITL confirmation.",
                ).model_dump()
                log_intent_and_outcome(
                    event_type="hitl_gate",
                    actor=actor,
                    session_id=session_id,
                    invocation_id=invocation_id,
                    intent="Issue high-value duty-of-care voucher",
                    intended_action=f"issue_hotel_meal_voucher({validated.pnr}, ${validated.amount_usd})",
                    outcome="rejected",
                    outcome_detail=res["message"],
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                return res
        else:
            existing = (
                repo.get_approval_request(approval_id)
                if approval_id
                else repo.find_matching_approval(
                    pnr=validated.pnr, action_type="ISSUE_VOUCHER"
                )
            )
            if existing and existing.status == "REJECTED":
                res = VoucherIssuanceOutput(
                    status="rejected",
                    pnr=validated.pnr,
                    approval_id=existing.approval_id,
                    voucher_type=validated.voucher_type,
                    amount_usd=validated.amount_usd,
                    vendor_name=validated.vendor_name,
                    requires_supervisor_approval=True,
                    message=(
                        f"Supervisor {existing.reviewer or 'ops'} rejected voucher request "
                        f"({existing.approval_id}): {existing.decision_reason or 'Policy denied'}."
                    ),
                ).model_dump()
                log_intent_and_outcome(
                    event_type="hitl_gate",
                    actor=actor,
                    session_id=session_id,
                    invocation_id=invocation_id,
                    intent="Issue high-value duty-of-care voucher",
                    intended_action=f"issue_hotel_meal_voucher({validated.pnr}, ${validated.amount_usd})",
                    outcome="rejected",
                    outcome_detail=res["message"],
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                return res

            payload_matches = bool(
                existing
                and existing.status == "APPROVED"
                and existing.pnr == validated.pnr
                and existing.payload.get("voucher_type") == validated.voucher_type
                and float(existing.payload.get("amount_usd", 0.0))
                >= validated.amount_usd
            )
            if not payload_matches:
                pending = (
                    existing
                    if (existing and existing.status == "PENDING")
                    else repo.create_approval_request(
                        pnr=validated.pnr,
                        action_type="ISSUE_VOUCHER",
                        summary=(
                            f"{validated.voucher_type} voucher ${validated.amount_usd:.2f} "
                            f"at {validated.vendor_name} for {reservation.passenger_name}"
                        ),
                        payload=validated.model_dump(),
                        estimated_cost_usd=validated.amount_usd,
                    )
                )
                res = VoucherIssuanceOutput(
                    status="awaiting_confirmation",
                    pnr=validated.pnr,
                    approval_id=pending.approval_id,
                    voucher_type=validated.voucher_type,
                    amount_usd=validated.amount_usd,
                    vendor_name=validated.vendor_name,
                    requires_supervisor_approval=True,
                    message=(
                        f"Voucher (${validated.amount_usd:.2f}) exceeds the "
                        f"${settings.voucher_auto_approve_limit_usd:.2f} auto-approval policy cap. "
                        f"Created pending HITL approval request {pending.approval_id}. "
                        "Supervisor approval is required before issuing this voucher."
                    ),
                ).model_dump()
                log_intent_and_outcome(
                    event_type="hitl_gate",
                    actor=actor,
                    session_id=session_id,
                    invocation_id=invocation_id,
                    intent="Issue high-value duty-of-care voucher",
                    intended_action=f"issue_hotel_meal_voucher({validated.pnr}, ${validated.amount_usd})",
                    outcome="awaiting_approval",
                    outcome_detail=res["message"],
                    latency_ms=(time.perf_counter() - started) * 1000,
                    metadata={"approval_id": pending.approval_id},
                )
                return res
            consumed_approval_id = existing.approval_id
            repo.consume_approval_request(consumed_approval_id)

    voucher = repo.record_voucher(
        pnr=validated.pnr,
        voucher_type=validated.voucher_type,
        amount_usd=validated.amount_usd,
        vendor_name=validated.vendor_name,
    )
    out = VoucherIssuanceOutput(
        status="issued",
        pnr=voucher.pnr,
        voucher_id=voucher.voucher_id,
        approval_id=consumed_approval_id or approval_id,
        voucher_type=voucher.voucher_type,
        amount_usd=voucher.amount_usd,
        vendor_name=voucher.vendor_name,
        requires_supervisor_approval=requires_supervisor,
        message=(
            f"Issued {voucher.voucher_type} voucher {voucher.voucher_id} for "
            f"${voucher.amount_usd:.2f} at {voucher.vendor_name}."
        ),
    ).model_dump()
    log_intent_and_outcome(
        event_type="tool_call",
        actor=actor,
        session_id=session_id,
        invocation_id=invocation_id,
        intent="Issue duty-of-care passenger voucher",
        intended_action=(
            f"issue_hotel_meal_voucher({voucher.pnr}, {voucher.voucher_type}, ${voucher.amount_usd})"
        ),
        outcome="succeeded",
        outcome_detail=f"voucher_id={voucher.voucher_id}",
        latency_ms=(time.perf_counter() - started) * 1000,
    )
    return out


def execute_passenger_rebooking(
    pnr: str,
    flight_numbers: list[str],
    cabin_class: Literal["Economy", "Business"] = "Economy",
    seat_preference: Literal["AISLE", "WINDOW", "ANY"] = "ANY",
    waive_fare_difference: bool = True,
    approval_id: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Commit an irreversible passenger PNR rebooking after Human-in-the-Loop approval.

    Purpose:
        Finalizes seat inventory decrements, updates the passenger's active
        flight segments, and generates a confirmed rebooking record (``RBK-...``).
        Because PNR modifications alter live seat inventory and revenue waivers,
        every call is gated by Human-in-the-Loop supervisor/passenger approval.

    Preconditions:
        1. Call ``calculate_rebooking_options`` first to select valid ``ON_TIME`` flights.
        2. Supply either an approved supervisor ``approval_id`` (``APR-...``) or an
           active ADK ``tool_confirmation``; otherwise this tool creates a pending
           ``ApprovalRecord`` and pauses without mutating the PNR.

    Side Effects:
        When authorized, decrements flight seat inventory and commits the new
        itinerary to SQLite/GCS. When unapproved, inserts a ``PENDING`` HITL
        approval request without modifying seat counts or PNR segments.

    Args:
        pnr: 6-character Passenger Name Record (e.g., ``SKY842``).
        flight_numbers: Ordered list of new flight numbers (e.g., ``['SC408']``).
        cabin_class: Cabin to reserve (``Economy`` or ``Business``).
        seat_preference: Requested seat type (``AISLE``, ``WINDOW``, or ``ANY``).
        waive_fare_difference: Whether involuntary disruption fare waiver applies.
        approval_id: Approved HITL request ID (``APR-...``) authorizing execution.
        tool_context: Injected ADK tool context.

    Returns:
        A ``RebookingExecutionOutput`` dictionary (``confirmed``,
        ``awaiting_confirmation``, or ``rejected``) or a ``ToolErrorOutput``
        dictionary with guided recovery steps.
    """
    started = time.perf_counter()
    session_id, invocation_id, actor = _ctx_ids(tool_context)

    try:
        validated = RebookingExecutionInput(
            pnr=pnr,
            flight_numbers=flight_numbers,
            cabin_class=cabin_class,
            seat_preference=seat_preference,
            waive_fare_difference=waive_fare_difference,
        )
    except ValidationError as exc:
        return _error(
            error_code="INVALID_REBOOKING_INPUT",
            message=str(exc),
            recovery_hint=(
                "Pass a 6-character PNR and a non-empty list of valid flight numbers "
                "from search_alternate_itineraries or calculate_rebooking_options."
            ),
            suggested_next_tools=["calculate_rebooking_options"],
        )

    repo = get_repository()
    reservation, _ = repo.get_reservation_by_pnr(validated.pnr)
    if reservation is None:
        return _error(
            error_code="PNR_NOT_FOUND",
            message=f"Reservation '{validated.pnr}' was not found.",
            recovery_hint="Verify the PNR with lookup_passenger_reservation.",
            suggested_next_tools=["lookup_passenger_reservation"],
        )

    fare_diff = 0.0
    for fn in validated.flight_numbers:
        flight = repo.get_flight(fn)
        if flight is None:
            return _error(
                error_code="FLIGHT_NOT_FOUND",
                message=f"Flight '{fn}' does not exist in schedule inventory.",
                recovery_hint="Search alternate itineraries to retrieve valid flight numbers.",
                suggested_next_tools=["search_alternate_itineraries"],
            )
        if flight.status != "ON_TIME":
            return _error(
                error_code="DISRUPTED_TARGET_FLIGHT",
                message=f"Cannot rebook onto flight '{fn}' because its status is '{flight.status}'.",
                recovery_hint="Choose an ON_TIME flight returned by calculate_rebooking_options.",
                suggested_next_tools=["calculate_rebooking_options"],
            )
        seats_avail = (
            flight.business_seats
            if validated.cabin_class == "Business"
            else flight.economy_seats
        )
        if seats_avail <= 0:
            return _error(
                error_code="INSUFFICIENT_SEAT_INVENTORY",
                message=f"Flight '{fn}' has 0 seats remaining in {validated.cabin_class}.",
                recovery_hint=(
                    "Call search_alternate_itineraries to pick another flight leg or cabin."
                ),
                suggested_next_tools=["search_alternate_itineraries"],
            )
        fare_diff += flight.base_fare_diff_usd

    # Check authorization (ADK native ToolConfirmation OR SQLite HITL ApprovalRecord)
    consumed_approval_id: str | None = None
    native_conf = (
        getattr(tool_context, "tool_confirmation", None)
        if tool_context is not None
        else None
    )
    if native_conf is not None:
        if not native_conf.confirmed:
            out = RebookingExecutionOutput(
                status="rejected",
                pnr=validated.pnr,
                flight_numbers=validated.flight_numbers,
                cabin_class=validated.cabin_class,
                fare_difference_usd=round(fare_diff, 2),
                fare_waived=validated.waive_fare_difference,
                message="Rebooking was rejected via HITL confirmation.",
            ).model_dump()
            log_intent_and_outcome(
                event_type="hitl_gate",
                actor=actor,
                session_id=session_id,
                invocation_id=invocation_id,
                intent="Commit passenger rebooking",
                intended_action=f"execute_passenger_rebooking({validated.pnr}, {validated.flight_numbers})",
                outcome="rejected",
                outcome_detail=out["message"],
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            return out
    else:
        existing = (
            repo.get_approval_request(approval_id)
            if approval_id
            else repo.find_matching_approval(
                pnr=validated.pnr, action_type="EXECUTE_REBOOKING"
            )
        )
        if existing and existing.status == "REJECTED":
            out = RebookingExecutionOutput(
                status="rejected",
                pnr=validated.pnr,
                approval_id=existing.approval_id,
                flight_numbers=validated.flight_numbers,
                cabin_class=validated.cabin_class,
                fare_difference_usd=round(fare_diff, 2),
                fare_waived=validated.waive_fare_difference,
                message=(
                    f"Rebooking request ({existing.approval_id}) was rejected by "
                    f"{existing.reviewer or 'supervisor'}: "
                    f"{existing.decision_reason or 'Approval denied'}."
                ),
            ).model_dump()
            log_intent_and_outcome(
                event_type="hitl_gate",
                actor=actor,
                session_id=session_id,
                invocation_id=invocation_id,
                intent="Commit passenger rebooking",
                intended_action=f"execute_passenger_rebooking({validated.pnr}, {validated.flight_numbers})",
                outcome="rejected",
                outcome_detail=out["message"],
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            return out

        payload_matches = bool(
            existing
            and existing.status == "APPROVED"
            and existing.pnr == validated.pnr
            and existing.payload.get("flight_numbers") == validated.flight_numbers
        )
        if not payload_matches:
            pending = (
                existing
                if (existing and existing.status == "PENDING")
                else repo.create_approval_request(
                    pnr=validated.pnr,
                    action_type="EXECUTE_REBOOKING",
                    summary=(
                        f"Rebook {reservation.passenger_name} ({validated.pnr}) onto "
                        f"{' -> '.join(validated.flight_numbers)} ({validated.cabin_class}, "
                        f"fare_diff=${fare_diff:.2f}, waived={validated.waive_fare_difference})"
                    ),
                    payload=validated.model_dump(),
                    estimated_cost_usd=fare_diff
                    if validated.waive_fare_difference
                    else 0.0,
                )
            )
            out = RebookingExecutionOutput(
                status="awaiting_confirmation",
                pnr=validated.pnr,
                approval_id=pending.approval_id,
                flight_numbers=validated.flight_numbers,
                cabin_class=validated.cabin_class,
                fare_difference_usd=round(fare_diff, 2),
                fare_waived=validated.waive_fare_difference,
                message=(
                    f"Irreversible PNR rebooking requires Human-in-the-Loop approval. "
                    f"Created pending approval request {pending.approval_id}. "
                    "Please approve in the SkyCare Supervisor Desk (/supervisor) before retrying."
                ),
            ).model_dump()
            log_intent_and_outcome(
                event_type="hitl_gate",
                actor=actor,
                session_id=session_id,
                invocation_id=invocation_id,
                intent="Commit passenger rebooking",
                intended_action=f"execute_passenger_rebooking({validated.pnr}, {validated.flight_numbers})",
                outcome="awaiting_approval",
                outcome_detail=out["message"],
                latency_ms=(time.perf_counter() - started) * 1000,
                metadata={"approval_id": pending.approval_id},
            )
            return out
        consumed_approval_id = existing.approval_id
        repo.consume_approval_request(consumed_approval_id)

    seat_map = {"AISLE": "14C", "WINDOW": "14A", "ANY": "14D"}
    seat_assignment = seat_map[validated.seat_preference]
    record = repo.record_rebooking(
        pnr=validated.pnr,
        new_flights=validated.flight_numbers,
        cabin_class=validated.cabin_class,
        seat_assignment=seat_assignment,
        fare_difference_usd=round(fare_diff, 2),
        fare_waived=validated.waive_fare_difference,
    )
    if tool_context is not None:
        tool_context.state["last_rebooking_id"] = record.rebooking_id

    out = RebookingExecutionOutput(
        status="confirmed",
        pnr=record.pnr,
        rebooking_id=record.rebooking_id,
        approval_id=consumed_approval_id or approval_id,
        flight_numbers=record.new_flights,
        cabin_class=record.cabin_class,
        seat_assignment=record.seat_assignment,
        fare_difference_usd=record.fare_difference_usd,
        fare_waived=record.fare_waived,
        message=(
            f"Confirmed rebooking {record.rebooking_id} for {record.pnr} onto "
            f"{' -> '.join(record.new_flights)} in {record.cabin_class} (seat {record.seat_assignment})."
        ),
    ).model_dump()
    log_intent_and_outcome(
        event_type="tool_call",
        actor=actor,
        session_id=session_id,
        invocation_id=invocation_id,
        intent="Commit approved passenger PNR rebooking",
        intended_action=f"execute_passenger_rebooking({record.pnr}, {record.new_flights})",
        outcome="succeeded",
        outcome_detail=f"rebooking_id={record.rebooking_id}",
        latency_ms=(time.perf_counter() - started) * 1000,
    )
    return out


execute_passenger_rebooking_tool = FunctionTool(
    execute_passenger_rebooking,
    require_confirmation=_requires_native_adk_confirmation,
)

issue_hotel_meal_voucher_tool = FunctionTool(
    issue_hotel_meal_voucher,
    require_confirmation=_requires_native_adk_confirmation,
)
