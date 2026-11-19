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

"""Deterministic PII and credential redaction for logs, telemetry, and memory."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PASSPORT_CONTEXT_RE = re.compile(
    r"(?i)\b(passport(?:\s*(?:is|no|num|number|#|:))?\s*[:=#]?\s*)([A-Z0-9]{6,10})\b"
)
_DOB_CONTEXT_RE = re.compile(
    r"(?i)\b((?:dob|date\s+of\s+birth|birth\s*date)(?:\s*is)?\s*[:=#]?\s*)(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\b"
)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
)
_CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_SECRET_TOKEN_RE = re.compile(
    r"(?i)\b(AIza[0-9A-Za-z\-_]{20,}|Bearer\s+[A-Za-z0-9\-._~+/]+=*|sk-[A-Za-z0-9]{16,})\b"
)

_SENSITIVE_KEY_EXACT = frozenset(
    {
        "api_key",
        "authorization",
        "birth_date",
        "card_number",
        "credit_card",
        "cvv",
        "date_of_birth",
        "dob",
        "email",
        "email_address",
        "loyalty_pin",
        "pan",
        "passport",
        "passport_number",
        "password",
        "phone",
        "phone_number",
        "secret",
        "ssn",
        "token",
    }
)


def _passes_luhn(digits: str) -> bool:
    """Validate candidate card numbers with Luhn checksum to spare flight/order IDs."""
    if not (13 <= len(digits) <= 19) or not digits.isdigit():
        return False
    total = 0
    reverse_digits = digits[::-1]
    for idx, ch in enumerate(reverse_digits):
        n = ord(ch) - 48
        if idx % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@dataclass
class RedactionResult:
    """Redacted value alongside a summary of matched entity categories."""

    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def redacted_total(self) -> int:
        """Total count of masked PII or secret spans."""
        return sum(self.counts.values())


def redact_text(text: str) -> RedactionResult:
    """Redact common PII and credential patterns from freeform text."""
    if not text:
        return RedactionResult(text=text)

    counts: dict[str, int] = {}

    def _bump(category: str) -> None:
        counts[category] = counts.get(category, 0) + 1

    # Secrets and tokens first so long keys aren't partially eaten by phone/card rules
    def _sub_secret(match: re.Match[str]) -> str:
        _bump("secret")
        return "[REDACTED_SECRET]"

    cleaned = _SECRET_TOKEN_RE.sub(_sub_secret, text)

    def _sub_ssn(match: re.Match[str]) -> str:
        _bump("ssn")
        return "[REDACTED_SSN]"

    cleaned = _SSN_RE.sub(_sub_ssn, cleaned)

    def _sub_passport(match: re.Match[str]) -> str:
        _bump("passport")
        return f"{match.group(1)}[REDACTED_PASSPORT]"

    cleaned = _PASSPORT_CONTEXT_RE.sub(_sub_passport, cleaned)

    def _sub_dob(match: re.Match[str]) -> str:
        _bump("dob")
        return f"{match.group(1)}[REDACTED_DOB]"

    cleaned = _DOB_CONTEXT_RE.sub(_sub_dob, cleaned)

    def _sub_email(match: re.Match[str]) -> str:
        _bump("email")
        return "[REDACTED_EMAIL]"

    cleaned = _EMAIL_RE.sub(_sub_email, cleaned)

    def _sub_card(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = re.sub(r"[^0-9]", "", raw)
        if _passes_luhn(digits):
            _bump("payment_card")
            return f"[REDACTED_CARD:****{digits[-4:]}]"
        return raw

    cleaned = _CARD_CANDIDATE_RE.sub(_sub_card, cleaned)

    def _sub_phone(match: re.Match[str]) -> str:
        _bump("phone")
        return "[REDACTED_PHONE]"

    cleaned = _PHONE_RE.sub(_sub_phone, cleaned)
    return RedactionResult(text=cleaned, counts=counts)


def redact_payload(value: Any, *, key_hint: str | None = None) -> Any:
    """Recursively redact PII and secrets inside structured JSON payloads."""
    if key_hint and key_hint.strip().lower() in _SENSITIVE_KEY_EXACT:
        if value is None or value == "":
            return value
        return f"[REDACTED_{key_hint.strip().upper()}]"

    if isinstance(value, str):
        return redact_text(value).text
    if isinstance(value, dict):
        return {str(k): redact_payload(v, key_hint=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item) for item in value]
    return value
