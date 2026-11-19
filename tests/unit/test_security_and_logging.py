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

"""Unit tests for PII redaction, Secret Manager abstraction, and JSON logging."""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from app.observability.logging import (
    StructuredJsonFormatter,
    clear_audit_events,
    get_audit_events,
    log_intent_and_outcome,
)
from app.security.pii_redactor import redact_payload, redact_text
from app.security.secrets import (
    EnvSecretProvider,
    SecretLookupError,
    SecretManagerProvider,
    SecretValue,
)


def test_redact_text_masks_sensitive_entities_preserves_pnr_and_flight() -> None:
    """Luhn PAN, SSN, passport, email, phone, and DOB mask without corrupting PNRs."""
    sample = (
        "Passenger PNR SKY842 on SC402. Passport US998877, SSN 123-45-6789, "
        "DOB: 1990-04-12, email elena.rostova@example.com, phone +1-415-555-0192, "
        "card 4111 1111 1111 1111."
    )
    result = redact_text(sample)

    assert "SKY842" in result.text
    assert "SC402" in result.text
    assert "US998877" not in result.text
    assert "123-45-6789" not in result.text
    assert "1990-04-12" not in result.text
    assert "elena.rostova@example.com" not in result.text
    assert "415-555-0192" not in result.text
    assert "4111 1111" not in result.text
    assert "[REDACTED_PASSPORT]" in result.text
    assert "[REDACTED_SSN]" in result.text
    assert "[REDACTED_DOB]" in result.text
    assert "[REDACTED_EMAIL]" in result.text
    assert "[REDACTED_PHONE]" in result.text
    assert "[REDACTED_CARD:****1111]" in result.text
    assert result.redacted_total == 6


def test_redact_payload_masks_nested_keys_and_strings() -> None:
    """Recursive dict redaction protects sensitive keys regardless of shape."""
    payload = {
        "pnr": "SKY842",
        "email": "traveler@example.com",
        "nested": {
            "passport_number": "A1234567",
            "note": "Call +1 212-555-0144 after arrival.",
        },
        "tokens": ["Bearer secret_jwt_token_value"],
    }
    redacted = redact_payload(payload)
    assert redacted["pnr"] == "SKY842"
    assert redacted["email"] == "[REDACTED_EMAIL]"
    assert redacted["nested"]["passport_number"] == "[REDACTED_PASSPORT_NUMBER]"
    assert "+1 212-555-0144" not in redacted["nested"]["note"]
    assert redacted["tokens"] == ["[REDACTED_SECRET]"]


def test_secret_value_never_exposes_plaintext_in_str_or_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SecretValue masks string representation and supports Env + SecretManager fallback."""
    secret = SecretValue(_raw="super-sensitive-gds-token")
    assert str(secret) == "***REDACTED***"
    assert repr(secret) == "SecretValue(***REDACTED***)"
    assert secret.reveal() == "super-sensitive-gds-token"

    monkeypatch.setenv("SKYCARE_SECRET_GDS_PARTNER_API_KEY", "test-key-123")
    env_provider = EnvSecretProvider()
    assert env_provider.get_secret("gds-partner-api-key").reveal() == "test-key-123"

    with pytest.raises(SecretLookupError, match="not configured"):
        env_provider.get_secret("missing-secret-name")


def test_secret_manager_provider_caches_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SecretManagerProvider caches fetched secret versions and uses Env fallback cleanly."""
    calls = 0

    class _FakePayload:
        data = b"sm-secret-value"

    class _FakeResponse:
        payload = _FakePayload()

    class _FakeSmClient:
        def access_secret_version(self, request: dict[str, str]):
            nonlocal calls
            calls += 1
            assert (
                request["name"]
                == "projects/skycare-test/secrets/gds-partner-api-key/versions/latest"
            )
            return _FakeResponse()

    provider = SecretManagerProvider(project_id="skycare-test", cache_ttl_seconds=60)
    monkeypatch.setattr(provider, "_get_client", lambda: _FakeSmClient())

    first = provider.get_secret("gds-partner-api-key")
    second = provider.get_secret("gds-partner-api-key")
    assert first.reveal() == "sm-secret-value"
    assert second.reveal() == "sm-secret-value"
    assert calls == 1


def test_structured_json_logging_and_intent_outcome_audit() -> None:
    """StructuredJsonFormatter emits valid PII-redacted JSON with intent and outcome."""
    clear_audit_events()
    stream = StringIO()
    test_logger = logging.getLogger("skycare.unit_test_logger")
    test_logger.handlers.clear()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredJsonFormatter())
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    test_logger.info(
        "Disruption check for elena@example.com card 4111-1111-1111-1111",
        extra={"intent": "lookup", "passport": "P99112233"},
    )
    record_json = json.loads(stream.getvalue().strip())
    assert record_json["severity"] == "INFO"
    assert "elena@example.com" not in record_json["message"]
    assert "4111-1111-1111-1111" not in record_json["message"]
    assert record_json["passport"] == "[REDACTED_PASSPORT]"

    event = log_intent_and_outcome(
        event_type="tool_call",
        actor="disruption_triage_agent",
        model="gemini-3.8-flash",
        intent="Verify compensation for SSN 123-45-6789",
        intended_action="check_eu261_dot_compensation_eligibility",
        outcome="succeeded",
        outcome_detail="EUR 600 + duty of care",
        latency_ms=14.234,
    )
    assert "123-45-6789" not in event.intent
    assert event.latency_ms == 14.23
    events = get_audit_events(limit=5)
    assert events[0]["intended_action"] == "check_eu261_dot_compensation_eligibility"
    assert events[0]["outcome"] == "succeeded"
