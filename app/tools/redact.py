"""Redact endpoint -- permanently removes sensitive content from PDFs.

Uses PyMuPDF to search for text patterns (email, phone, custom text,
or user-provided regex) and apply true content-stream redaction.
Redacted text is permanently deleted, not just visually hidden.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key
from app.http_utils import (
    file_response,
    parse_legacy_options,
    read_upload_bytes,
    run_legacy_service,
)
from app.services.pdf_tools import redact_pdf

router = APIRouter()
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
UploadedFile = Annotated[UploadFile, File(...)]
LegacyOptions = Annotated[str, Form()]


@router.post("/redact")
async def redact(
    file: UploadedFile,
    _key: ApiKeyDep,
    options: LegacyOptions = "{}",
) -> Response:
    """Accept a PDF and return it with matched content permanently redacted."""
    opts = parse_legacy_options(options)
    strategy = opts.get("strategy", "email")
    custom_text = opts.get("customText", "")
    regex_pattern = opts.get("regexPattern", "")
    content = await read_upload_bytes(file, legacy=True)
    result = await run_legacy_service(
        redact_pdf,
        content,
        strategy=strategy,
        custom_text=custom_text,
        regex_pattern=regex_pattern,
    )
    return file_response(result, "application/pdf", file.filename, "output.pdf")
