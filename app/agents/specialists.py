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

"""Role-specialized sub-agents with Strategic Model Routing (Flash vs. Pro)."""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from app.core.config import get_settings
from app.tools.airline_tools import (
    calculate_rebooking_options,
    check_eu261_dot_compensation_eligibility,
    execute_passenger_rebooking_tool,
    issue_hotel_meal_voucher_tool,
    lookup_passenger_reservation,
    search_alternate_itineraries,
)


def _retry_options() -> types.HttpRetryOptions:
    return types.HttpRetryOptions(attempts=3)


def build_disruption_triage_agent() -> Agent:
    """Fast Flash-tier specialist for PNR inspection and regulatory triage."""
    settings = get_settings()
    return Agent(
        name="disruption_triage_agent",
        model=Gemini(
            model=settings.flash_model,
            retry_options=_retry_options(),
        ),
        description=(
            "Fast operational triage specialist (Flash tier). Delegate here for initial "
            "PNR lookup, flight disruption root-cause summary, and EU261/UK261/US-DOT "
            "compensation or duty-of-care eligibility verification."
        ),
        static_instruction=(
            "You are the SkyCare Disruption Triage Specialist.\n"
            "Rules:\n"
            "1. Always verify facts exclusively via `lookup_passenger_reservation` and "
            "`check_eu261_dot_compensation_eligibility`. Never guess flight statuses or rules.\n"
            "2. If a tool returns `status='error'`, follow its `recovery_hint` explicitly.\n"
            "3. Never echo raw payment card numbers, passport numbers, SSNs, or API keys.\n"
            "4. Present clear, structured findings covering PNR status, disrupted segment, "
            "loyalty tier, passenger preferences, and regulatory entitlements."
        ),
        instruction=(
            "Inspect the passenger's booking and regulatory eligibility accurately. "
            "Active session PNR if known: {active_pnr?}. If alternate multi-leg routing "
            "analysis is also requested by the traveler, summarize triage facts cleanly so "
            "the coordinator can route complex itinerary planning to recovery_planner_agent."
        ),
        tools=[
            lookup_passenger_reservation,
            check_eu261_dot_compensation_eligibility,
        ],
    )


def build_recovery_planner_agent() -> Agent:
    """Deep Pro-tier planner for complex multi-leg optimization and trade-offs."""
    settings = get_settings()
    return Agent(
        name="recovery_planner_agent",
        model=Gemini(
            model=settings.pro_model,
            retry_options=_retry_options(),
        ),
        description=(
            "Strategic Pro-tier optimization planner (`gemini-3.1-pro-preview`). Delegate here "
            "to evaluate alternate nonstop and multi-leg itineraries, weigh connection "
            "misconnect risks, align with remembered seat/meal preferences, and compute "
            "ranked recovery options."
        ),
        static_instruction=(
            "You are the SkyCare Senior Recovery Planner powered by Gemini Pro.\n"
            "Rules:\n"
            "1. Evaluate candidate routes objectively using `search_alternate_itineraries`, "
            "`calculate_rebooking_options`, and `check_eu261_dot_compensation_eligibility`.\n"
            "2. Compare options transparently by arrival time, stops/misconnect risk, cabin "
            "availability, passenger loyalty status, fare-difference waivers, and remembered "
            "seat/dietary preferences.\n"
            "3. Highlight the top-scored recommendation with exact flight numbers and times.\n"
            "4. Never promise a seat is confirmed until `execute_passenger_rebooking` succeeds "
            "with Human-in-the-Loop approval."
        ),
        instruction=(
            "Analyze all viable recovery routes for active PNR {active_pnr?} "
            "({active_origin?} -> {active_destination?}, cabin={active_cabin?}). "
            "Compare nonstop vs connecting trade-offs clearly and recommend the optimal choice."
        ),
        tools=[
            search_alternate_itineraries,
            calculate_rebooking_options,
            check_eu261_dot_compensation_eligibility,
        ],
    )


def build_passenger_care_agent() -> Agent:
    """Controlled Flash-tier fulfillment specialist honoring HITL approval gates."""
    settings = get_settings()
    return Agent(
        name="passenger_care_agent",
        model=Gemini(
            model=settings.flash_model,
            retry_options=_retry_options(),
        ),
        description=(
            "Controlled execution specialist (Flash tier). Delegate here when committing "
            "a passenger PNR rebooking or issuing hotel, meal, or ground-transport "
            "duty-of-care vouchers subject to Human-in-the-Loop policy gates."
        ),
        static_instruction=(
            "You are the SkyCare Passenger Care & Fulfillment Specialist.\n"
            "Rules:\n"
            "1. Before issuing any voucher, verify duty-of-care eligibility via "
            "`check_eu261_dot_compensation_eligibility`.\n"
            "2. Call `issue_hotel_meal_voucher` or `execute_passenger_rebooking` strictly "
            "with validated parameters.\n"
            "3. Human-in-the-Loop Enforcement: if either tool returns "
            "`status='awaiting_confirmation'`, STOP immediately, do NOT invent an approval, "
            "clearly state the exact `approval_id` (e.g., APR-XXXXXXXX), cost/itinerary, "
            "and direct the supervisor/operator to approve it in the SkyCare Approval Drawer.\n"
            "4. If a tool returns `status='rejected'`, communicate the supervisor decision "
            "respectfully and do not retry without a new approved authorization.\n"
            "5. If a tool returns `status='confirmed'` or `status='issued'`, summarize the "
            "exact confirmation ID (`RBK-...` or `VCH-...`) and itinerary/vendor details."
        ),
        instruction=(
            "Complete the requested voucher or rebooking action for active PNR {active_pnr?} "
            "(recommended itinerary: {recommended_itinerary_id?}). Strictly report the real "
            "tool status without fabricating IDs."
        ),
        tools=[
            lookup_passenger_reservation,
            check_eu261_dot_compensation_eligibility,
            issue_hotel_meal_voucher_tool,
            execute_passenger_rebooking_tool,
        ],
    )
