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

"""Local LLM-as-judge for `custom_response_quality` (see eval_config.yaml)."""

from __future__ import annotations

import threading

from google import genai
from google.genai import types
from pydantic import BaseModel

_local = threading.local()


class _Verdict(BaseModel):
    score: int  # 1-5
    explanation: str


def _client() -> genai.Client:
    """One client per grading thread to reuse TLS and ADC credentials cleanly."""
    client = getattr(_local, "client", None)
    if client is None:
        import os

        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        has_api_key = bool(
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        )
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
        use_vertex = (
            os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower()
            in {"1", "true", "yes"}
            or os.environ.get("GOOGLE_GENAI_USE_ENTERPRISE", "").lower()
            in {"1", "true", "yes"}
            or (not has_api_key and bool(project))
        )
        client = _local.client = (
            genai.Client(vertexai=True, project=project, location=location)
            if (use_vertex and project)
            else genai.Client()
        )
    return client


def evaluate(instance: dict) -> dict[str, int | str]:
    """Score airline disruption recovery accuracy, policy compliance, and clarity."""
    reference = instance.get("reference")
    rubric = (
        "Grade SkyCare Concierge's final response on a 1-5 scale (1 poor, 5 excellent) "
        "for factual grounding to tool outputs, airline disruption policy accuracy "
        "(EU261 / US DOT), strict respect for Human-in-the-Loop approval gates, and clarity."
    )
    if reference:
        rubric += (
            " Penalize fabricated flight numbers, fabricated confirmation or approval IDs, "
            "bypassed approval gates, or factual disagreement with the expected answer below."
        )
    prompt = (
        f"You are an expert QA evaluator for an airline disruption recovery agent. {rubric}\n"
        f"User Prompt: {instance.get('prompt', '')}\n"
        f"Final Response: {instance.get('response', '')}\n"
    )
    if reference:
        prompt += f"Expected Answer (ground truth): {reference}\n"
    prompt += f"Full Agent Trace: {instance.get('agent_data', '')}\n"

    response = _client().models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=_Verdict,
        ),
    )
    verdict = response.parsed
    if not isinstance(verdict, _Verdict):
        return {"score": 0, "explanation": response.text or ""}
    return {
        "score": max(1, min(5, verdict.score)),
        "explanation": verdict.explanation,
    }
