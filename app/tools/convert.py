"""Convert-to-PDF endpoint -- office docs via LibreOffice, images via img2pdf."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key
from app.http_utils import (
    file_response,
    filename_stem,
    parse_legacy_options,
    read_upload_bytes,
    run_legacy_service,
)
from app.services.pdf_tools import convert_to_pdf

router = APIRouter()
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
UploadedFile = Annotated[UploadFile, File(...)]
LegacyOptions = Annotated[str, Form()]


@router.post("/convert")
async def convert(
    file: UploadedFile,
    _key: ApiKeyDep,
    options: LegacyOptions = "{}",
) -> Response:
    """Convert office documents or images to PDF."""
    parse_legacy_options(options)
    content = await read_upload_bytes(file, legacy=True)
    pdf_bytes = await run_legacy_service(convert_to_pdf, content, file.content_type, file.filename)
    return file_response(
        pdf_bytes,
        "application/pdf",
        f"{filename_stem(file.filename)}.pdf",
        "output.pdf",
    )
