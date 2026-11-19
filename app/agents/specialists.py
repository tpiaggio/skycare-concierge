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

"""Role-specialized sub-agents with low-latency Gemini 3.8 + Gemini 4 routing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.planners import BuiltInPlanner
from google.genai import types

from app.core.config import get_settings
from app.storage.repository import get_repository
from app.tools.airline_tools import (
    calculate_rebooking_options,
    check_eu261_dot_compensation_eligibility,
    execute_passenger_rebooking_tool,
    issue_hotel_meal_voucher_tool,
    lookup_passenger_reservation,
    search_alternate_itineraries,
)

logger = logging.getLogger(__name__)

_PNR_TOKEN_RE = re.compile(r"\b(?=[A-Za-z0-9]{6}\b)(?:[A-Za-z]{2,4}\d{2,4})\b")


def _retry_options() -> types.HttpRetryOptions:
    return types.HttpRetryOptions(attempts=3)


def low_thinking_planner() -> BuiltInPlanner:
    """Cap thinking effort at LOW to eliminate excessive reasoning latency."""
    return BuiltInPlanner(
        thinking_config=types.ThinkingConfig(thinking_level="LOW"),
    )


def build_stream_gemini(
    *,
    model: str,
    fallback_model: str | None = None,
    hedge_delay_seconds: float = 8.0,
    first_chunk_timeout_seconds: float = 15.0,
    stall_retry_attempts: int = 2,
) -> StreamBackedGemini:
    """Create a StreamBackedGemini instance with parallel hedging and model fallback."""
    return StreamBackedGemini(
        model=model,
        retry_options=_retry_options(),
        hedge_delay_seconds=hedge_delay_seconds,
        first_chunk_timeout_seconds=first_chunk_timeout_seconds,
        stall_retry_attempts=stall_retry_attempts,
        fallback_model=fallback_model,
    )


def _clone_request(
    llm_request: LlmRequest, *, model_name: str | None = None
) -> LlmRequest:
    updates: dict[str, Any] = {
        "contents": list(llm_request.contents),
        "config": (
            llm_request.config.model_copy(deep=True)
            if llm_request.config is not None
            else None
        ),
    }
    if model_name is not None:
        updates["model"] = model_name
    return llm_request.model_copy(update=updates)


async def _close_generator(gen: AsyncGenerator[LlmResponse, None]) -> None:
    with contextlib.suppress(Exception):
        await gen.aclose()


async def prewarm_pnr_context_callback(
    callback_context: CallbackContext,
) -> types.Content | None:
    """Deterministically pre-fetch PNR, regulatory verdict, and routes before LLM calls."""
    callback_context.state.setdefault("prefetched_pnr_context", "")
    if not callback_context.state.get("is_user_prompt_safe", True):
        return None

    user_text = ""
    if callback_context.user_content and callback_context.user_content.parts:
        user_text = " ".join(
            part.text
            for part in callback_context.user_content.parts
            if isinstance(part.text, str) and part.text
        )

    repo = get_repository()
    target_pnr: str | None = None
    reservation = None
    for candidate in _PNR_TOKEN_RE.findall(user_text):
        upper = candidate.upper()
        found, _ = repo.get_reservation_by_pnr(upper)
        if found is not None:
            target_pnr = found.pnr
            reservation = found
            break

    if target_pnr is None:
        active = callback_context.state.get("active_pnr")
        if isinstance(active, str) and active:
            found, _ = repo.get_reservation_by_pnr(active.upper())
            if found is not None:
                target_pnr = found.pnr
                reservation = found

    if target_pnr is None or reservation is None:
        callback_context.state["prefetched_pnr_context"] = ""
        return None

    res_info = lookup_passenger_reservation(target_pnr, tool_context=callback_context)
    comp_info = check_eu261_dot_compensation_eligibility(
        target_pnr, tool_context=callback_context
    )
    bundle: dict[str, Any] = {
        "reservation": res_info,
        "compensation": comp_info,
    }
    if reservation.rebooking_status != "ON_SCHEDULE":
        bundle["rebooking_options"] = calculate_rebooking_options(
            target_pnr,
            cabin_class=reservation.cabin_class,
            tool_context=callback_context,
        )

    callback_context.state["prefetched_pnr_context"] = json.dumps(
        bundle, ensure_ascii=False, separators=(",", ":")
    )
    return None


class StreamBackedGemini(Gemini):
    """Gemini wrapper with streaming RPCs, parallel hedging, and fallback failover.

    Uses ``generate_content_stream`` even when ADK requests ``stream=False``,
    launches a parallel hedged request if the first chunk stalls past
    ``hedge_delay_seconds``, enforces ``first_chunk_timeout_seconds``, and fails
    over to ``fallback_model`` when configured so preview stalls never hang UI turns.
    """

    hedge_delay_seconds: float = 8.0
    first_chunk_timeout_seconds: float = 15.0
    stall_retry_attempts: int = 2
    fallback_model: str | None = None

    async def _open_and_first_chunk(
        self, llm_client: Gemini, request: LlmRequest
    ) -> tuple[AsyncGenerator[LlmResponse, None] | None, LlmResponse | None]:
        gen = Gemini.generate_content_async(llm_client, request, stream=True)
        try:
            first_response = await gen.__anext__()
            return gen, first_response
        except StopAsyncIteration:
            await _close_generator(gen)
            return None, None
        except BaseException:
            await _close_generator(gen)
            raise

    async def _cleanup_hedge_tasks(
        self,
        tasks: set[asyncio.Task[tuple[Any, Any]]],
        *,
        winner: asyncio.Task[tuple[Any, Any]] | None = None,
    ) -> None:
        for task in tasks:
            if task is winner:
                continue
            if not task.done():
                task.cancel()
        await asyncio.gather(
            *[t for t in tasks if t is not winner], return_exceptions=True
        )
        for task in tasks:
            if task is winner or task.cancelled():
                continue
            if task.done() and task.exception() is None:
                loser_gen, _ = task.result()
                if loser_gen is not None:
                    await _close_generator(loser_gen)

    async def _acquire_hedged_stream(
        self, llm_request: LlmRequest
    ) -> tuple[AsyncGenerator[LlmResponse, None] | None, LlmResponse | None]:
        """Run primary attempt with optional parallel hedge before hard timeout."""
        primary_task = asyncio.create_task(
            self._open_and_first_chunk(self, _clone_request(llm_request))
        )
        tasks: set[asyncio.Task[tuple[Any, Any]]] = {primary_task}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.first_chunk_timeout_seconds

        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=self.hedge_delay_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if done and primary_task.exception() is None:
                return primary_task.result()
            if done and primary_task.exception() is not None and not pending:
                exc = primary_task.exception()
                if exc is not None:
                    raise exc

            logger.warning(
                "No first chunk after %.1fs on model=%s; launching parallel hedged request.",
                self.hedge_delay_seconds,
                self.model,
            )
            hedge_task = asyncio.create_task(
                self._open_and_first_chunk(self, _clone_request(llm_request))
            )
            tasks.add(hedge_task)
            pending = tasks.copy()

            while pending:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    break
                last_exc: BaseException | None = None
                for finished in done:
                    exc = finished.exception()
                    if exc is None:
                        res = finished.result()
                        await self._cleanup_hedge_tasks(tasks, winner=finished)
                        return res
                    last_exc = exc
                if not pending and last_exc is not None:
                    raise last_exc

            raise TimeoutError(
                f"Both hedged requests timed out after {self.first_chunk_timeout_seconds:.1f}s on {self.model}"
            )
        except BaseException:
            await self._cleanup_hedge_tasks(tasks)
            raise

    async def _forward_stream(
        self,
        gen: AsyncGenerator[LlmResponse, None],
        first_response: LlmResponse,
        stream: bool,
    ) -> AsyncGenerator[LlmResponse, None]:
        try:
            if stream or not first_response.partial:
                yield first_response
            async for response in gen:
                if stream or not response.partial:
                    yield response
        finally:
            await _close_generator(gen)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        attempts = max(1, self.stall_retry_attempts)
        use_hedge = (
            attempts >= 2
            and 0 < self.hedge_delay_seconds < self.first_chunk_timeout_seconds
        )

        if use_hedge:
            try:
                gen, first_response = await self._acquire_hedged_stream(llm_request)
                if gen is None or first_response is None:
                    return
                async for item in self._forward_stream(gen, first_response, stream):
                    yield item
                return
            except TimeoutError:
                logger.warning(
                    "Hedged attempts timed out after %.1fs on model=%s%s.",
                    self.first_chunk_timeout_seconds,
                    self.model,
                    f"; failing over to {self.fallback_model}"
                    if self.fallback_model
                    else "",
                )
                if not self.fallback_model:
                    raise
        else:
            for attempt in range(attempts):
                is_last_without_fallback = (
                    attempt == attempts - 1 and not self.fallback_model
                )
                try:
                    coro = self._open_and_first_chunk(self, _clone_request(llm_request))
                    gen, first_response = (
                        await coro
                        if is_last_without_fallback
                        else await asyncio.wait_for(
                            coro, timeout=self.first_chunk_timeout_seconds
                        )
                    )
                    if gen is None or first_response is None:
                        return
                    async for item in self._forward_stream(gen, first_response, stream):
                        yield item
                    return
                except TimeoutError:
                    logger.warning(
                        "Stalled waiting %.1fs for first chunk from model=%s (attempt %d/%d); retrying.",
                        self.first_chunk_timeout_seconds,
                        self.model,
                        attempt + 1,
                        attempts,
                    )
                    continue

        if not self.fallback_model:
            raise TimeoutError(
                f"Model {self.model} stalled across {attempts} attempts."
            )

        fallback_llm = Gemini(
            model=self.fallback_model, retry_options=self.retry_options
        )
        gen, first_response = await self._open_and_first_chunk(
            fallback_llm, _clone_request(llm_request, model_name=self.fallback_model)
        )
        if gen is None or first_response is None:
            return
        async for item in self._forward_stream(gen, first_response, stream):
            yield item


def build_disruption_triage_agent() -> Agent:
    """Fast Flash-tier specialist for PNR inspection and regulatory triage."""
    settings = get_settings()
    return Agent(
        name="disruption_triage_agent",
        model=build_stream_gemini(
            model=settings.flash_model, fallback_model=settings.lite_model
        ),
        planner=low_thinking_planner(),
        description=(
            "Fast operational triage specialist (Flash tier). Delegate here for initial "
            "PNR lookup, flight disruption root-cause summary, and EU261/UK261/US-DOT "
            "compensation or duty-of-care eligibility verification."
        ),
        static_instruction=(
            "You are the SkyCare Disruption Triage Specialist.\n"
            "Rules:\n"
            "1. Use Pre-fetched PNR context when already populated for the requested PNR "
            "without redundantly re-calling lookup tools. Call `lookup_passenger_reservation` "
            "and `check_eu261_dot_compensation_eligibility` only for a missing/unloaded PNR. "
            "Never guess flight statuses or rules.\n"
            "2. If a tool returns `status='error'`, follow its `recovery_hint` explicitly.\n"
            "3. Never echo raw payment card numbers, passport numbers, SSNs, or API keys.\n"
            "4. Present clear, structured findings covering PNR status, disrupted segment, "
            "loyalty tier, passenger preferences, and regulatory entitlements.\n"
            "5. Always reply in the user's language (English or Spanish) while preserving exact "
            "PNR codes, flight numbers, and IATA codes."
        ),
        instruction=(
            "Inspect the passenger's booking and regulatory eligibility accurately. "
            "Active session PNR if known: {active_pnr?}. "
            "Pre-fetched PNR context: {prefetched_pnr_context?}. "
            "If the traveler ALSO asked for alternate flights, nonstop/connecting options, "
            "rebooking recommendations, or a recovery plan in the same turn, NEVER end the "
            "turn after triage—immediately call "
            "`transfer_to_agent(agent_name='recovery_planner_agent')` so the Gemini 4 "
            "planner delivers the unified compensation and ranked route answer."
        ),
        tools=[
            lookup_passenger_reservation,
            check_eu261_dot_compensation_eligibility,
        ],
    )


def build_recovery_planner_agent() -> Agent:
    """Deep Gemini 4 planner for complex multi-leg optimization and trade-offs."""
    settings = get_settings()
    return Agent(
        name="recovery_planner_agent",
        model=build_stream_gemini(
            model=settings.pro_model, fallback_model=settings.flash_model
        ),
        planner=low_thinking_planner(),
        generate_content_config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=2048,
        ),
        description=(
            f"Strategic Gemini 4 route optimization planner (`{settings.pro_model}`). "
            "Delegate here to evaluate alternate nonstop and multi-leg itineraries, weigh "
            "connection misconnect risks, align with remembered seat/meal preferences, "
            "and compute ranked recovery options."
        ),
        static_instruction=(
            "You are the SkyCare Senior Recovery Planner powered by Gemini 4.\n"
            "Rules:\n"
            "1. Zero-Redundancy Single-Turn Synthesis: Check `Pre-fetched PNR context` "
            "and recent tool results in context first—if reservation, compensation, and "
            "rebooking options for the active PNR are already populated, DO NOT call any "
            "lookup/rebooking tools again; proceed directly to comparing options and delivering "
            "the final recommendation. Call tools only if data for the requested PNR or a "
            "different cabin class (`search_alternate_itineraries`) is missing.\n"
            "2. Briefly summarize EU261/UK261/US-DOT compensation and waivers when asked, then "
            "compare alternate options by arrival time, stops/misconnect risk, cabin availability, "
            "and remembered seat/dietary preferences.\n"
            "3. Highlight the top-scored recommendation with exact flight numbers and times.\n"
            "4. Never promise a seat is confirmed until `execute_passenger_rebooking` succeeds "
            "with Human-in-the-Loop approval.\n"
            "5. Keep responses crisp and concise (<=150 words, short bullets) and respond "
            "in the user's language (English or Spanish) while keeping exact flight numbers, "
            "IATA airport codes, and PNR/itinerary IDs unchanged."
        ),
        instruction=(
            "Analyze all viable recovery routes for active PNR {active_pnr?} "
            "({active_origin?} -> {active_destination?}, cabin={active_cabin?}). "
            "Pre-fetched PNR context: {prefetched_pnr_context?}. "
            "Compare nonstop vs connecting trade-offs clearly and recommend the optimal choice."
        ),
        tools=[
            lookup_passenger_reservation,
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
        model=build_stream_gemini(
            model=settings.flash_model, fallback_model=settings.lite_model
        ),
        planner=low_thinking_planner(),
        description=(
            "Controlled execution specialist (Flash tier). Delegate here when committing "
            "a passenger PNR rebooking or issuing hotel, meal, or ground-transport "
            "duty-of-care vouchers subject to Human-in-the-Loop policy gates."
        ),
        static_instruction=(
            "You are the SkyCare Passenger Care & Fulfillment Specialist.\n"
            "Rules:\n"
            "1. Check `Pre-fetched PNR context` first for duty-of-care eligibility when already "
            "populated; call `check_eu261_dot_compensation_eligibility` only if missing.\n"
            "2. Call `issue_hotel_meal_voucher` or `execute_passenger_rebooking` directly "
            "and strictly with validated parameters.\n"
            "3. Human-in-the-Loop Enforcement: if either tool returns "
            "`status='awaiting_confirmation'`, STOP immediately, do NOT invent an approval, "
            "clearly state the exact `approval_id` (e.g., APR-XXXXXXXX), cost/itinerary, "
            "and direct the supervisor/operator to approve it in the SkyCare Supervisor Desk (`/supervisor`).\n"
            "4. If a tool returns `status='rejected'`, communicate the supervisor decision "
            "respectfully and do not retry without a new approved authorization.\n"
            "5. If a tool returns `status='confirmed'` or `status='issued'`, summarize the "
            "exact confirmation ID (`RBK-...` or `VCH-...`) and itinerary/vendor details.\n"
            "6. Respond in the user's language (English or Spanish), translating status guidance "
            "naturally while keeping exact IDs (`APR-...`, `VCH-...`, `RBK-...`) and PNR codes verbatim."
        ),
        instruction=(
            "Complete the requested voucher or rebooking action for active PNR {active_pnr?} "
            "(recommended itinerary: {recommended_itinerary_id?}). "
            "Pre-fetched PNR context: {prefetched_pnr_context?}. "
            "Strictly report the real tool status without fabricating IDs."
        ),
        tools=[
            lookup_passenger_reservation,
            check_eu261_dot_compensation_eligibility,
            issue_hotel_meal_voucher_tool,
            execute_passenger_rebooking_tool,
        ],
    )
