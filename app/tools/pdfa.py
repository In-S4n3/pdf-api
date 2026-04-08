"""PDF/A conversion endpoint -- converts PDF to archival PDF/A format.

Uses Ghostscript pdfwrite with PDF/A conformance settings.
Supports PDF/A-1b, 2b (default), and 3b conformance.
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
from app.services.pdf_tools import convert_pdf_to_pdfa

router = APIRouter()
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
UploadedFile = Annotated[UploadFile, File(...)]
LegacyOptions = Annotated[str, Form()]


@router.post("/pdfa")
async def pdfa(
    file: UploadedFile,
    _key: ApiKeyDep,
    options: LegacyOptions = "{}",
) -> Response:
    """Accept a PDF and return a PDF/A-compliant version."""
    opts = parse_legacy_options(options)
    conformance = opts.get("conformance", "pdfa-2b")
    content = await read_upload_bytes(file, legacy=True)
    pdf_bytes = await run_legacy_service(convert_pdf_to_pdfa, content, conformance)
    return file_response(pdf_bytes, "application/pdf", file.filename, "output.pdf")
