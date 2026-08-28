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


# Matches TudoPDF's server-tool ceiling. Keeping the backend at the same lower
# bound means direct callers cannot bypass the browser/proxy guard and spend the
# Cloud Run request budget on files the product itself refuses.
DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 * 1024

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
    # Production is always fail-closed. STRICT_API_KEY remains useful for
    # exercising the same posture in development, but cannot disable it in prod.
    strict_api_key = environment == "production" or _read_bool("STRICT_API_KEY", default=False)
    return Settings(
        environment=environment,
        api_key=api_key,
        debug=environment == "development",
        strict_api_key=strict_api_key,
        max_upload_bytes=_read_optional_int("MAX_UPLOAD_BYTES") or DEFAULT_MAX_UPLOAD_BYTES,
        cors_allowed_origins=_read_csv("CORS_ALLOWED_ORIGINS") or DEFAULT_CORS_ORIGINS,
    )


_settings = get_settings()
ENVIRONMENT = _settings.environment
API_KEY = _settings.api_key
DEBUG = _settings.debug
