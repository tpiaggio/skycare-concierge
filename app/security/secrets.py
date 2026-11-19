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

"""Secret management abstraction backed by Cloud Secret Manager and env fallback."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol

from app.core.config import get_settings


class SecretLookupError(RuntimeError):
    """Raised when a required secret cannot be resolved."""


@dataclass(frozen=True)
class SecretValue:
    """Masked container preventing accidental plaintext disclosure in logs/reprs."""

    _raw: str

    def reveal(self) -> str:
        """Explicitly unwrap the secret inside trusted backend code only."""
        return self._raw

    def __str__(self) -> str:
        return "***REDACTED***"

    def __repr__(self) -> str:
        return "SecretValue(***REDACTED***)"


class SecretProvider(Protocol):
    """Contract for resolving application secrets by logical ID."""

    def get_secret(self, secret_id: str, version: str = "latest") -> SecretValue:
        """Resolve and wrap a secret without exposing plaintext in repr."""


class EnvSecretProvider:
    """Environment-backed secret provider for local dev, CI, and dry-run modes."""

    def __init__(self, prefix: str = "SKYCARE_SECRET_") -> None:
        self._prefix = prefix

    def get_secret(self, secret_id: str, version: str = "latest") -> SecretValue:
        del version  # env vars are unversioned
        env_key = f"{self._prefix}{secret_id.upper().replace('-', '_')}"
        raw = os.environ.get(env_key) or os.environ.get(
            secret_id.upper().replace("-", "_")
        )
        if not raw:
            raise SecretLookupError(
                f"Secret '{secret_id}' is not configured in environment ({env_key}). "
                "Set it in .env for local runs or provision it in Google Secret Manager."
            )
        return SecretValue(_raw=raw)


class SecretManagerProvider:
    """GCP Secret Manager provider with TTL caching and env-var fallback for local dev."""

    def __init__(
        self,
        project_id: str,
        *,
        cache_ttl_seconds: int = 300,
        fallback: EnvSecretProvider | None = None,
    ) -> None:
        self._project_id = project_id
        self._cache_ttl = cache_ttl_seconds
        self._fallback = fallback or EnvSecretProvider()
        self._cache: dict[tuple[str, str], tuple[float, SecretValue]] = {}
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def get_secret(self, secret_id: str, version: str = "latest") -> SecretValue:
        cache_key = (secret_id, version)
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < self._cache_ttl:
            return cached[1]

        resource_name = (
            f"projects/{self._project_id}/secrets/{secret_id}/versions/{version}"
        )
        try:
            client = self._get_client()
            response = client.access_secret_version(request={"name": resource_name})
            payload = response.payload.data.decode("utf-8")
            secret = SecretValue(_raw=payload)
            self._cache[cache_key] = (now, secret)
            return secret
        except Exception as exc:
            try:
                secret = self._fallback.get_secret(secret_id, version=version)
                self._cache[cache_key] = (now, secret)
                return secret
            except SecretLookupError:
                raise SecretLookupError(
                    f"Unable to read Secret Manager resource '{resource_name}': {exc}. "
                    "Ensure Terraform secret resources and roles/secretmanager.secretAccessor are applied."
                ) from exc


def get_secret_provider() -> SecretProvider:
    """Select Secret Manager in cloud deployments and Env fallback in local/dry-run mode."""
    settings = get_settings()
    if (
        settings.dry_run
        or settings.use_in_memory_session
        or not settings.google_cloud_project
    ):
        return EnvSecretProvider()
    return SecretManagerProvider(
        project_id=settings.google_cloud_project,
        cache_ttl_seconds=settings.secret_cache_ttl_seconds,
    )
