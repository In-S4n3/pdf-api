"""PDF-to-Image endpoint -- renders first page as PNG or JPEG at 300 DPI.

Uses PyMuPDF page.get_pixmap(dpi=300) for high-quality rendering
with native PNG/JPEG output via tobytes().
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key
from app.http_utils import (
    file_response,
    filename_stem,
    parse_legacy_options,
    read_upload_bytes,
    run_legacy_service,
)
from app.services.pdf_tools import pdf_first_page_to_image

router = APIRouter()
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
UploadedFile = Annotated[UploadFile, File(...)]
LegacyOptions = Annotated[str, Form()]


@router.post("/pdf-to-image")
async def pdf_to_image(
    file: UploadedFile,
    _key: ApiKeyDep,
    options: LegacyOptions = "{}",
) -> Response:
    """Accept a PDF and return the first page as an image."""
    opts = parse_legacy_options(options)
    fmt = opts.get("format", "png")
    if fmt not in ("png", "jpeg"):
        raise HTTPException(status_code=400, detail="Format must be 'png' or 'jpeg'")

    content = await read_upload_bytes(file, legacy=True)
    result, media_type, ext = await run_legacy_service(pdf_first_page_to_image, content, fmt)
    return file_response(
        result,
        media_type,
        f"{filename_stem(file.filename)}.{ext}",
        f"output.{ext}",
    )
