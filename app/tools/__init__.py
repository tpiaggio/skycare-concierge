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

"""SkyCare Concierge tool suite and strict Pydantic contracts."""

from app.tools.airline_tools import (
    calculate_rebooking_options,
    check_eu261_dot_compensation_eligibility,
    execute_passenger_rebooking,
    execute_passenger_rebooking_tool,
    issue_hotel_meal_voucher,
    issue_hotel_meal_voucher_tool,
    lookup_passenger_reservation,
    search_alternate_itineraries,
)
from app.tools.schemas import (
    CompensationEligibilityOutput,
    FlightSearchOutput,
    PassengerLookupOutput,
    RebookingExecutionOutput,
    RebookingProposalOutput,
    ToolErrorOutput,
    VoucherIssuanceOutput,
)

__all__ = [
    "CompensationEligibilityOutput",
    "FlightSearchOutput",
    "PassengerLookupOutput",
    "RebookingExecutionOutput",
    "RebookingProposalOutput",
    "ToolErrorOutput",
    "VoucherIssuanceOutput",
    "calculate_rebooking_options",
    "check_eu261_dot_compensation_eligibility",
    "execute_passenger_rebooking",
    "execute_passenger_rebooking_tool",
    "issue_hotel_meal_voucher",
    "issue_hotel_meal_voucher_tool",
    "lookup_passenger_reservation",
    "search_alternate_itineraries",
]
