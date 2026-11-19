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

"""Vertex AI Agent Platform Memory Bank configuration for SkyCare Concierge."""

from __future__ import annotations

from agentplatform._genai.types.common import (
    ManagedTopicEnum,
)
from agentplatform._genai.types.common import (
    MemoryBankCustomizationConfig as CustomizationConfig,
)
from agentplatform._genai.types.common import (
    MemoryBankCustomizationConfigMemoryTopic as MemoryTopic,
)
from agentplatform._genai.types.common import (
    MemoryBankCustomizationConfigMemoryTopicCustomMemoryTopic as CustomMemoryTopic,
)
from agentplatform._genai.types.common import (
    MemoryBankCustomizationConfigMemoryTopicManagedMemoryTopic as ManagedMemoryTopic,
)
from agentplatform._genai.types.common import (
    ReasoningEngineContextSpecMemoryBankConfig as MemoryBankConfig,
)


def build_memory_bank_config() -> MemoryBankConfig:
    """Build non-PII traveler-preference Memory Bank config for Agent Platform."""
    return MemoryBankConfig(
        customization_configs=[
            CustomizationConfig(
                memory_topics=[
                    MemoryTopic(
                        managed_memory_topic=ManagedMemoryTopic(
                            managed_topic_enum=ManagedTopicEnum.USER_PREFERENCES,
                        ),
                    ),
                    MemoryTopic(
                        managed_memory_topic=ManagedMemoryTopic(
                            managed_topic_enum=ManagedTopicEnum.KEY_CONVERSATION_DETAILS,
                        ),
                    ),
                    MemoryTopic(
                        managed_memory_topic=ManagedMemoryTopic(
                            managed_topic_enum=ManagedTopicEnum.EXPLICIT_INSTRUCTIONS,
                        ),
                    ),
                    MemoryTopic(
                        custom_memory_topic=CustomMemoryTopic(
                            label="traveler_seating_and_cabin_preferences",
                            description=(
                                "Non-PII seating preferences (aisle, window, bulkhead), "
                                "dietary meal preferences, home airport, and connection risk "
                                "tolerance. Never store passport numbers, payment cards, "
                                "phone numbers, emails, or SSNs."
                            ),
                        ),
                    ),
                ],
            ),
        ],
    )
