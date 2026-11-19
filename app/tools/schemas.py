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

"""Strict Pydantic I/O contracts for SkyCare Concierge tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ToolErrorOutput(BaseModel):
    """Structured error envelope with actionable LLM recovery guidance."""

    status: Literal["error"] = "error"
    error_code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(
        description="Human-readable reason the operation could not run."
    )
    recovery_hint: str = Field(
        description="Step-by-step guidance explaining how the agent should recover."
    )
    suggested_next_tools: list[str] = Field(
        default_factory=list,
        description="Recommended follow-up tool names to call next.",
    )


class LookupReservationInput(BaseModel):
    """Validated passenger PNR lookup parameters."""

    pnr: str = Field(
        min_length=5,
        max_length=8,
        description="6-character uppercase airline Passenger Name Record (e.g., SKY842).",
    )

    @field_validator("pnr")
    @classmethod
    def _validate_pnr(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned.isalnum() or len(cleaned) != 6:
            raise ValueError(
                "PNR must be exactly 6 alphanumeric characters (e.g., SKY842)."
            )
        return cleaned


class FlightSegmentSchema(BaseModel):
    """Serialized flight segment details returned to reasoning agents."""

    flight_number: str
    origin: str
    destination: str
    departure_utc: str
    arrival_utc: str
    status: str
    delay_minutes: int
    disruption_cause: str
    jurisdiction: str
    distance_km: int
    economy_seats: int
    business_seats: int
    base_fare_diff_usd: float
    carrier: str


class PassengerLookupOutput(BaseModel):
    """Validated PNR lookup payload including disruption status and preferences."""

    status: Literal["ok"] = "ok"
    pnr: str
    passenger_name: str
    loyalty_tier: str
    cabin_class: str
    origin: str
    destination: str
    rebooking_status: str
    disrupted_flight: str | None = None
    disruption_summary: str
    preferences: dict[str, object] = Field(default_factory=dict)
    segments: list[FlightSegmentSchema]


class ItinerarySearchInput(BaseModel):
    """Validated alternate itinerary search parameters."""

    origin: str = Field(description="3-letter uppercase IATA origin airport code.")
    destination: str = Field(
        description="3-letter uppercase IATA destination airport code."
    )
    cabin_class: Literal["Economy", "Business"] = Field(
        default="Economy",
        description="Requested travel cabin ('Economy' or 'Business').",
    )

    @field_validator("origin", "destination")
    @classmethod
    def _validate_iata(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if len(cleaned) != 3 or not cleaned.isalpha():
            raise ValueError(
                f"Airport code '{value}' must be a valid 3-letter IATA code (e.g., JFK, ZRH)."
            )
        return cleaned


class ItineraryCandidate(BaseModel):
    """Single candidate reaccommodation path."""

    itinerary_id: str
    stops: int
    departure_utc: str
    arrival_utc: str
    fare_difference_usd: float
    available_seats: int
    legs: list[FlightSegmentSchema]


class FlightSearchOutput(BaseModel):
    """Search result containing matching nonstop and one-stop recovery paths."""

    status: Literal["ok"] = "ok"
    origin: str
    destination: str
    cabin_class: str
    options_count: int
    itineraries: list[ItineraryCandidate]


class CompensationEligibilityOutput(BaseModel):
    """Deterministic EU261 / UK261 / US DOT compensation & duty-of-care verdict."""

    status: Literal["ok"] = "ok"
    pnr: str
    regulation_framework: str
    eligible_for_cash_compensation: bool
    cash_compensation_eur: int
    eligible_for_duty_of_care: bool
    hotel_allowance_usd: float
    meal_allowance_usd: float
    waive_fare_difference: bool
    rationale: str


class ScoredRebookingOption(BaseModel):
    """Evaluated reaccommodation candidate ranked by disruption recovery score."""

    itinerary_id: str
    flight_numbers: list[str]
    stops: int
    arrival_utc: str
    passenger_net_cost_usd: float
    fare_difference_usd: float
    fare_waived: bool
    recovery_score: float = Field(
        ge=0.0,
        le=100.0,
        description="Composite score (0-100) accounting for arrival time, nonstop preference, and risk.",
    )
    recommendation_notes: str


class RebookingProposalOutput(BaseModel):
    """Ranked reaccommodation options tailored to the passenger itinerary and tier."""

    status: Literal["ok"] = "ok"
    pnr: str
    loyalty_tier: str
    recommended_itinerary_id: str
    options: list[ScoredRebookingOption]


class VoucherInput(BaseModel):
    """Validated duty-of-care voucher request parameters."""

    pnr: str
    voucher_type: Literal["HOTEL", "MEAL", "GROUND_TRANSPORT"]
    amount_usd: float = Field(gt=0.0, le=500.0)
    vendor_name: str = Field(min_length=2, max_length=80)

    @field_validator("pnr")
    @classmethod
    def _validate_pnr(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned.isalnum() or len(cleaned) != 6:
            raise ValueError("PNR must be 6 alphanumeric characters.")
        return cleaned


class VoucherIssuanceOutput(BaseModel):
    """Result of voucher issuance or HITL confirmation pause."""

    status: Literal["issued", "awaiting_confirmation", "rejected"]
    pnr: str
    voucher_id: str | None = None
    approval_id: str | None = None
    voucher_type: str
    amount_usd: float
    vendor_name: str
    requires_supervisor_approval: bool
    message: str


class RebookingExecutionInput(BaseModel):
    """Validated parameters to commit an irreversible PNR rebooking."""

    pnr: str
    flight_numbers: list[str] = Field(min_length=1, max_length=3)
    cabin_class: Literal["Economy", "Business"] = "Economy"
    seat_preference: Literal["AISLE", "WINDOW", "ANY"] = "ANY"
    waive_fare_difference: bool = True

    @field_validator("pnr")
    @classmethod
    def _validate_pnr(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned.isalnum() or len(cleaned) != 6:
            raise ValueError("PNR must be 6 alphanumeric characters.")
        return cleaned

    @field_validator("flight_numbers")
    @classmethod
    def _validate_flights(cls, flights: list[str]) -> list[str]:
        cleaned = [f.strip().upper() for f in flights if f.strip()]
        if not cleaned:
            raise ValueError(
                "At least one flight number is required (e.g., ['SC408'])."
            )
        return cleaned


class RebookingExecutionOutput(BaseModel):
    """Result of committing or gating a passenger itinerary change."""

    status: Literal["confirmed", "awaiting_confirmation", "rejected"]
    pnr: str
    rebooking_id: str | None = None
    approval_id: str | None = None
    flight_numbers: list[str]
    cabin_class: str
    seat_assignment: str | None = None
    fare_difference_usd: float = 0.0
    fare_waived: bool = True
    message: str
