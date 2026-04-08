"""Protect endpoint -- encrypts PDF with AES-256 password protection.

Uses pikepdf's Encryption API with R=6 (AES-256) to apply password
protection with restricted permissions: printing allowed, editing
and text/image extraction blocked.
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
from app.services.pdf_tools import protect_pdf

router = APIRouter()
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
UploadedFile = Annotated[UploadFile, File(...)]
LegacyOptions = Annotated[str, Form()]


@router.post("/protect")
async def protect(
    file: UploadedFile,
    _key: ApiKeyDep,
    options: LegacyOptions = "{}",
) -> Response:
    """Accept a PDF and password, return an AES-256 encrypted PDF."""
    opts = parse_legacy_options(options)
    password = opts.get("userPassword", "")
    content = await read_upload_bytes(file, legacy=True)
    result = await run_legacy_service(protect_pdf, content, password)
    return file_response(result, "application/pdf", file.filename, "output.pdf")
