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

"""Process-wide ADK session, memory, and artifact services shared by all surfaces."""

from __future__ import annotations

import functools
import logging
import os

import agentplatform
from agentplatform._genai.types.common import ReasoningEngineContextSpec
from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.adk.cli.service_registry import get_service_registry
from google.adk.cli.utils.service_factory import (
    create_memory_service_from_options,
    create_session_service_from_options,
)
from google.adk.memory import InMemoryMemoryService
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from app.app_utils.memory_config import build_memory_bank_config

SESSION_SERVICE_URI = "shared://session"
ARTIFACT_SERVICE_URI = "shared://artifact"
MEMORY_SERVICE_URI = "shared://memory"

_AGENT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_logger = logging.getLogger("skycare.services")


def _use_in_memory() -> bool:
    return os.environ.get("USE_IN_MEMORY_SESSION", "").lower() in (
        "true",
        "1",
        "yes",
    ) or os.environ.get("INTEGRATION_TEST", "").lower() in ("true", "1", "yes")


@functools.cache
def _get_or_create_agent_runtime():
    """Find or create the regional Agent Platform runtime for Sessions + Memory Bank."""
    default_agent_name = "skycare-concierge"
    agent_name = os.environ.get("AGENT_ENGINE_SESSION_NAME", default_agent_name)
    client = agentplatform.Client(location=agent_runtime_location())
    existing_agents = list(
        client.runtimes.list(config={"filter": f"display_name={agent_name}"})
    )
    if existing_agents:
        return existing_agents[0]
    context_spec = ReasoningEngineContextSpec(
        memory_bank_config=build_memory_bank_config(),
    )
    return client.runtimes.create(
        config={
            "display_name": agent_name,
            "context_spec": context_spec,
        }
    )


@functools.cache
def get_session_service():
    """Process-wide session service shared across every serving surface."""
    if not _use_in_memory():
        runtime = _get_or_create_agent_runtime()
        return create_session_service_from_options(
            base_dir=_AGENT_DIR,
            session_service_uri=f"agentengine://{runtime.api_resource.name}",
        )
    return InMemorySessionService()


@functools.cache
def get_memory_service():
    """Process-wide cross-session memory service shared by ADK runner & Web UI."""
    if not _use_in_memory():
        try:
            runtime = _get_or_create_agent_runtime()
            return create_memory_service_from_options(
                base_dir=_AGENT_DIR,
                memory_service_uri=f"agentengine://{runtime.api_resource.name}",
            )
        except Exception as exc:
            _logger.warning(
                "Falling back to InMemoryMemoryService for runtime %s: %s",
                agent_runtime_location(),
                exc,
            )
    return InMemoryMemoryService()


def agent_runtime_location() -> str:
    """Region for the session Agent Runtime; it doesn't support 'global'."""
    if location := os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION"):
        return location
    location = os.environ.get("GOOGLE_CLOUD_LOCATION")
    if location and location != "global":
        return location
    return "us-east1"


@functools.cache
def get_artifact_service():
    """Process-wide artifact service: GCS when a bucket is set, else in-memory."""
    if bucket := os.environ.get("LOGS_BUCKET_NAME"):
        return GcsArtifactService(bucket_name=bucket)
    return InMemoryArtifactService()


_registry = get_service_registry()
_registry.register_session_service("shared", lambda uri, **kw: get_session_service())
_registry.register_artifact_service("shared", lambda uri, **kw: get_artifact_service())
_registry.register_memory_service("shared", lambda uri, **kw: get_memory_service())
