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

"""Live ADK Runner integration test covering airline PNR triage streaming."""

from __future__ import annotations

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import app as adk_app


def test_agent_stream() -> None:
    """Verify full ADK App Runner streaming response on a real disruption lookup."""
    session_service = InMemorySessionService()
    memory_service = InMemoryMemoryService()

    session = session_service.create_session_sync(
        user_id="test_user", app_name=adk_app.name
    )
    runner = Runner(
        app=adk_app,
        session_service=session_service,
        memory_service=memory_service,
    )

    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text="Look up PNR SKY842 and summarize which flight was disrupted and why."
            )
        ],
    )

    events = list(
        runner.run(
            new_message=message,
            user_id="test_user",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )
    assert len(events) > 0, "Expected at least one event"

    text_chunks = [
        part.text
        for event in events
        if event.content and event.content.parts
        for part in event.content.parts
        if part.text
    ]
    combined = " ".join(text_chunks)
    assert combined.strip(), "Expected non-empty streaming text output"
    assert "SC402" in combined or "SKY842" in combined
