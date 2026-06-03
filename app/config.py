"""Application configuration helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _read_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_optional_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return int(value)


def _read_csv(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


# Matches the frontend FE limit (50 MB) so a missing/misconfigured env var
# never leaves the API accepting arbitrarily large uploads (DoS surface).
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Production CORS allowlist used when CORS_ALLOWED_ORIGINS is unset. Keeps a
# misconfigured deploy from silently denying every browser request.
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "https://tudopdf.app",
    "https://www.tudopdf.app",
)


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    api_key: str
    debug: bool
    strict_api_key: bool
    max_upload_bytes: int
    cors_allowed_origins: tuple[str, ...]


def get_settings() -> Settings:
    environment = os.environ.get("ENVIRONMENT", "production").strip().lower() or "production"
    api_key = os.environ.get("API_KEY", "").strip()
    return Settings(
        environment=environment,
        api_key=api_key,
        debug=environment == "development",
        strict_api_key=_read_bool("STRICT_API_KEY", default=False),
        max_upload_bytes=_read_optional_int("MAX_UPLOAD_BYTES") or DEFAULT_MAX_UPLOAD_BYTES,
        cors_allowed_origins=_read_csv("CORS_ALLOWED_ORIGINS") or DEFAULT_CORS_ORIGINS,
    )


_settings = get_settings()
ENVIRONMENT = _settings.environment
API_KEY = _settings.api_key
DEBUG = _settings.debug
