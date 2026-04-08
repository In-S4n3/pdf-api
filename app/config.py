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


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    api_key: str
    debug: bool
    strict_api_key: bool
    max_upload_bytes: int | None


def get_settings() -> Settings:
    environment = os.environ.get("ENVIRONMENT", "production").strip().lower() or "production"
    api_key = os.environ.get("API_KEY", "").strip()
    return Settings(
        environment=environment,
        api_key=api_key,
        debug=environment == "development",
        strict_api_key=_read_bool("STRICT_API_KEY", default=False),
        max_upload_bytes=_read_optional_int("MAX_UPLOAD_BYTES"),
    )


_settings = get_settings()
ENVIRONMENT = _settings.environment
API_KEY = _settings.api_key
DEBUG = _settings.debug
