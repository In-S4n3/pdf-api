"""Fill form endpoint -- fills AcroForm fields and flattens to static PDF."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi import Form as FastAPIForm
from fastapi.responses import Response

from app.auth import verify_api_key
from app.http_utils import (
    file_response,
    parse_legacy_options,
    read_upload_bytes,
    run_legacy_service,
)
from app.services.pdf_tools import fill_form_pdf

router = APIRouter()
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
UploadedFile = Annotated[UploadFile, File(...)]
LegacyOptions = Annotated[str, FastAPIForm()]


@router.post("/fill-form")
async def fill_form(
    file: UploadedFile,
    _key: ApiKeyDep,
    options: LegacyOptions = "{}",
) -> Response:
    """Accept a PDF and field values, return filled+flattened PDF."""
    opts = parse_legacy_options(options)
    field_values = opts.get("fields", {})
    content = await read_upload_bytes(file, legacy=True)
    result = await run_legacy_service(
        fill_form_pdf,
        content,
        field_values,
        strict_unknown_fields=False,
    )
    return file_response(result, "application/pdf", file.filename, "output.pdf")
