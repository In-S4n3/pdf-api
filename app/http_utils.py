"""Shared HTTP helpers for legacy and v2 routes."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.api_errors import ApiError
from app.config import get_settings


def sanitize_filename(filename: str | None, default: str) -> str:
    """Return a safe attachment filename."""
    candidate = Path(filename or default).name or default
    cleaned = candidate.replace('"', "").replace("\r", "").replace("\n", "")
    return cleaned or default


def filename_stem(filename: str | None, default: str = "output") -> str:
    """Return a safe filename stem without extension."""
    safe_name = sanitize_filename(filename, f"{default}.pdf")
    stem = Path(safe_name).stem
    return stem or default


def attachment_headers(filename: str | None, default: str) -> dict[str, str]:
    """Build a safe attachment header."""
    safe_name = sanitize_filename(filename, default)
    return {"Content-Disposition": f'attachment; filename="{safe_name}"'}


def file_response(content: bytes, media_type: str, filename: str | None, default: str) -> Response:
    """Create a binary attachment response."""
    return Response(
        content=content,
        media_type=media_type,
        headers=attachment_headers(filename, default),
    )


def parse_options_json(raw_options: str) -> dict[str, Any]:
    """Parse an options JSON string into a dictionary."""
    try:
        parsed = json.loads(raw_options or "{}")
    except JSONDecodeError as exc:
        raise ApiError(
            status_code=400,
            code="invalid_options",
            message="Options must be valid JSON.",
        ) from exc

    if not isinstance(parsed, dict):
        raise ApiError(
            status_code=400,
            code="invalid_options",
            message="Options payload must be a JSON object.",
        )

    return parsed


def parse_legacy_options(raw_options: str) -> dict[str, Any]:
    """Legacy wrapper that preserves HTTPException-based error handling."""
    try:
        return parse_options_json(raw_options)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


_READ_CHUNK_SIZE = 64 * 1024  # 64 KB


async def read_upload_bytes(file: UploadFile, *, legacy: bool = False) -> bytes:
    """Read upload bytes in chunks, aborting as soon as the size limit is exceeded.

    Reading the entire payload before checking length lets an attacker OOM the
    container with a single oversized request. Streaming and bailing early caps
    peak memory at `max_upload_bytes + chunk_size`.
    """
    settings = get_settings()
    max_bytes = settings.max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            error = ApiError(
                status_code=413,
                code="file_too_large",
                message=(
                    f"Uploaded file exceeds the configured limit of "
                    f"{max_bytes} bytes."
                ),
            )
            if legacy:
                raise HTTPException(status_code=error.status_code, detail=error.message) from error
            raise error
        chunks.append(chunk)
    return b"".join(chunks)


async def run_service[T](service: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking service in the threadpool."""
    return await run_in_threadpool(partial(service, *args, **kwargs))


async def run_legacy_service[T](service: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking service and convert ApiError into HTTPException."""
    try:
        return await run_service(service, *args, **kwargs)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
