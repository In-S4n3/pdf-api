"""Compress endpoint -- reduces PDF file size using PyMuPDF.

Applies image rewriting (downsampling + JPEG recompression),
garbage collection, stream deflation, and object stream compression.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key
from app.http_utils import file_response, read_upload_bytes, run_legacy_service
from app.services.pdf_tools import compress_pdf

router = APIRouter()
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
UploadedFile = Annotated[UploadFile, File(...)]


@router.post("/compress")
async def compress(
    file: UploadedFile,
    _key: ApiKeyDep,
) -> Response:
    """Accept a PDF and return a compressed version."""
    content = await read_upload_bytes(file, legacy=True)
    result = await run_legacy_service(compress_pdf, content)
    return file_response(result, "application/pdf", file.filename, "output.pdf")
