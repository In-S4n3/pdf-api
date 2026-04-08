"""OCR endpoint -- adds searchable text layer to scanned PDFs.

Uses OCRmyPDF CLI (subprocess) with --skip-text to OCR pages
that lack a text layer. Supports 8 languages via Tesseract.
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
from app.services.pdf_tools import ocr_pdf

router = APIRouter()
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
UploadedFile = Annotated[UploadFile, File(...)]
LegacyOptions = Annotated[str, Form()]


@router.post("/ocr")
async def ocr(
    file: UploadedFile,
    _key: ApiKeyDep,
    options: LegacyOptions = "{}",
) -> Response:
    """Accept a PDF and return it with an OCR text layer."""
    opts = parse_legacy_options(options)
    language = opts.get("language", "english")
    content = await read_upload_bytes(file, legacy=True)
    pdf_bytes = await run_legacy_service(ocr_pdf, content, language)
    return file_response(pdf_bytes, "application/pdf", file.filename, "output.pdf")
