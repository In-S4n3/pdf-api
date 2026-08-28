"""Shared HTTP helpers for legacy and v2 routes."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from functools import partial
from json import JSONDecodeError
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.api_errors import ApiError
from app.config import get_settings

#: Long enough for any real filename. The name reaches us from a caller, not
#: from a disk, so nothing else bounds it — and h11 caps a response's headers at
#: 16 KiB, past which the response never sends at all.
MAX_FILENAME_LENGTH = 200

#: A "%" that a lenient Content-Disposition parser would read as an escape.
_PERCENT_ESCAPE = re.compile("%(?=[0-9a-fA-F]{2})")


def sanitize_filename(filename: str | None, default: str) -> str:
    """Return a safe attachment filename."""
    # Browsers may send Windows paths, and HTTP headers cannot contain control
    # characters. Normalise separators before taking the basename and strip
    # anything that could break a quoted Content-Disposition value.
    candidate = Path((filename or default).replace("\\", "/")).name or default
    # NFC, not whatever arrived: macOS uploads decompose «ã» into a + U+0303,
    # and Windows and Linux both expect the composed form on the way back out.
    cleaned = "".join(
        character
        for character in unicodedata.normalize("NFC", candidate).replace('"', "")
        if ord(character) >= 32 and ord(character) != 127
    )
    if not cleaned:
        return default
    if len(cleaned) <= MAX_FILENAME_LENGTH:
        return cleaned

    # Truncate the stem, never the extension — the extension is what the caller
    # reads back. A "." this far from the end is not an extension, and half of
    # one is worse than none.
    dot = cleaned.rfind(".")
    extension = cleaned[dot:] if dot > 0 and len(cleaned) - dot <= 16 else ""
    return cleaned[: MAX_FILENAME_LENGTH - len(extension)] + extension


def filename_stem(filename: str | None, default: str = "output") -> str:
    """Return a safe filename stem without extension."""
    safe_name = sanitize_filename(filename, f"{default}.pdf")
    stem = Path(safe_name).stem
    return stem or default


def _fold_to_ascii(name: str) -> str:
    """NFKD then drop the leftovers: «ç» decomposes to c + U+0327, and the
    combining mark falls away, so the fallback keeps a readable name.

    "%XX" goes too: RFC 6266 Appendix D asks senders to keep it out of the
    plain `filename`, since some parsers unescape it and others do not. The
    real name travels in `filename*`.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return _PERCENT_ESCAPE.sub("_", folded)


def attachment_headers(filename: str | None, default: str) -> dict[str, str]:
    """Build an ASCII-safe header with an RFC 5987 UTF-8 filename.

    The ASCII `filename` comes first: RFC 6266 Appendix D asks for that order,
    "due to parsing problems in some existing implementations".
    """
    safe_name = sanitize_filename(filename, default)
    ascii_name = _fold_to_ascii(safe_name)
    # Judge the stem, not the whole string: a CJK name folds down to a bare
    # ".pdf", which passes an emptiness test and is still no name at all.
    # `rsplit`, not `Path.stem` — pathlib reads ".pdf" as a dotfile and hands
    # the whole thing back as the stem.
    if not ascii_name.rsplit(".", 1)[0].strip(" ."):
        # Borrow the stem from the default but keep the real extension: it is
        # what the caller opens the file with, and what our own media type was
        # chosen from — the two must agree.
        fallback_name = _fold_to_ascii(sanitize_filename(default, "output.pdf"))
        dot = ascii_name.rfind(".")
        extension = ascii_name[dot:] if dot >= 0 and len(ascii_name) - dot <= 16 else ""
        ascii_name = (
            fallback_name.rsplit(".", 1)[0] + extension if extension else fallback_name
        )
    encoded_name = quote(safe_name, safe="!#$&+-.^_`|~")
    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{encoded_name}"
        )
    }


def file_response(content: bytes, media_type: str, filename: str | None, default: str) -> Response:
    """Create a binary attachment response."""
    return Response(
        content=content,
        media_type=media_type,
        headers=attachment_headers(filename, default),
    )


def parse_options_json(
    raw_options: str,
    *,
    invalid_json_message: str = "Options must be valid JSON.",
    invalid_object_message: str = "Options payload must be a JSON object.",
) -> dict[str, Any]:
    """Parse an options JSON string into a dictionary."""
    try:
        parsed = json.loads(raw_options or "{}")
    except JSONDecodeError as exc:
        raise ApiError(
            status_code=400,
            code="invalid_options",
            message=invalid_json_message,
        ) from exc

    if not isinstance(parsed, dict):
        raise ApiError(
            status_code=400,
            code="invalid_options",
            message=invalid_object_message,
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
            message = (
                f"Uploaded file exceeds the configured limit of {max_bytes} bytes."
                if legacy
                else f"O ficheiro excede o limite configurado de {max_bytes} bytes."
            )
            error = ApiError(
                status_code=413,
                code="file_too_large",
                message=message,
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
