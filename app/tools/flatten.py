"""Flatten endpoint -- bakes annotations and form fields into permanent page content.

Uses PyMuPDF bake() to convert all annotations (highlights, comments,
stamps, ink, etc.) and form widgets (text fields, checkboxes, radio
buttons, dropdowns) into static page content.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key
from app.http_utils import file_response, read_upload_bytes, run_legacy_service
from app.services.pdf_tools import flatten_pdf

router = APIRouter()
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
UploadedFile = Annotated[UploadFile, File(...)]


@router.post("/flatten")
async def flatten(
    file: UploadedFile,
    _key: ApiKeyDep,
) -> Response:
    """Accept a PDF and return it with annotations/forms baked in."""
    content = await read_upload_bytes(file, legacy=True)
    result = await run_legacy_service(flatten_pdf, content)
    return file_response(result, "application/pdf", file.filename, "output.pdf")
