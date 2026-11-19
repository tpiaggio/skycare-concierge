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

"""Centralized configuration and strategic model routing settings."""

from __future__ import annotations

import functools
import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def ensure_vertex_ai_env() -> None:
    """Load .env and normalize Vertex AI flags when ADC is used without an API key."""
    load_dotenv()
    has_api_key = bool(
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    )
    use_enterprise = _env_bool("GOOGLE_GENAI_USE_ENTERPRISE", False)
    has_project = bool(os.environ.get("GOOGLE_CLOUD_PROJECT"))
    if not has_api_key and (use_enterprise or has_project):
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")


def build_genai_client() -> genai.Client:
    """Construct a Google GenAI Client using Vertex AI ADC when no API key is set."""
    ensure_vertex_ai_env()
    has_api_key = bool(
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    )
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    use_vertex = _env_bool("GOOGLE_GENAI_USE_VERTEXAI", False) or (
        not has_api_key and bool(project)
    )
    if use_vertex and project:
        return genai.Client(vertexai=True, project=project, location=location)
    return genai.Client()


class SkyCareSettings(BaseModel):
    """Runtime settings for SkyCare Concierge."""

    app_name: str = "app"
    root_agent_name: str = "skycare_concierge"
    service_name: str = "skycare-concierge"

    # Verified live Gemini models on location="global" for low-latency strategic routing:
    # - flash_model: high-throughput coordinator, triage, fulfillment, guardrails, summarizer
    # - pro_model: Gemini 4 Hydrogen deep multi-leg planner (overridable via SKYCARE_PRO_MODEL)
    flash_model: str = Field(
        default_factory=lambda: os.environ.get(
            "SKYCARE_FLASH_MODEL", "gemini-3.8-flash"
        )
    )
    pro_model: str = Field(
        default_factory=lambda: os.environ.get("SKYCARE_PRO_MODEL", "gemini-4-hydrogen")
    )
    lite_model: str = Field(
        default_factory=lambda: os.environ.get("SKYCARE_LITE_MODEL", "gemini-3.5-flash")
    )

    # Human-in-the-Loop policy bounds
    voucher_auto_approve_limit_usd: float = 150.0
    require_confirmation_for_rebooking: bool = True

    # Context compaction & caching configuration
    compaction_token_threshold: int = 24000
    compaction_retention_size: int = 6
    context_cache_min_tokens: int = 16384
    context_cache_ttl_seconds: int = 1800

    # Cloud & storage configuration (Option #1: Agent Platform + SQLite/GCS)
    google_cloud_project: str | None = Field(
        default_factory=lambda: os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    google_cloud_location: str = Field(
        default_factory=lambda: os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    )
    agent_runtime_name: str = Field(
        default_factory=lambda: os.environ.get(
            "AGENT_ENGINE_SESSION_NAME", "skycare-concierge"
        )
    )
    logs_bucket_name: str | None = Field(
        default_factory=lambda: os.environ.get("LOGS_BUCKET_NAME") or None
    )
    db_path: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("SKYCARE_DB_PATH", "/tmp/skycare-concierge/skycare.db")
        )
    )

    # Feature & safety toggles
    use_in_memory_session: bool = Field(
        default_factory=lambda: _env_bool("USE_IN_MEMORY_SESSION", False)
    )
    dry_run: bool = Field(
        default_factory=lambda: (
            _env_bool("SKYCARE_DRY_RUN", False) or _env_bool("INTEGRATION_TEST", False)
        )
    )
    enable_safety_judge_llm: bool = Field(
        default_factory=lambda: _env_bool("SKYCARE_SAFETY_JUDGE_LLM", True)
    )
    use_cloud_dlp: bool = Field(
        default_factory=lambda: _env_bool("SKYCARE_USE_CLOUD_DLP", False)
    )
    memory_redaction_enabled: bool = True
    secret_cache_ttl_seconds: int = 300


@functools.cache
def get_settings() -> SkyCareSettings:
    """Return cached runtime settings after normalizing .env and Vertex AI env."""
    ensure_vertex_ai_env()
    return SkyCareSettings()
