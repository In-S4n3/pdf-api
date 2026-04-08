"""Shared API error types."""

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ApiError(Exception):
    """Typed application error that can be mapped to HTTP responses."""

    status_code: int
    code: str
    message: str
    details: dict[str, Any] | list[dict[str, Any]] | None = None
