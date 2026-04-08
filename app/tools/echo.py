"""Echo endpoint -- returns the uploaded file unmodified.

Used to validate the multipart upload / binary response contract
without any PDF processing. Critical for Phase 9-10 integration testing.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key
from app.http_utils import file_response, read_upload_bytes

router = APIRouter()
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
UploadedFile = Annotated[UploadFile, File(...)]


@router.post("/echo")
async def echo(
    file: UploadedFile,
    _key: ApiKeyDep,
) -> Response:
    """Accept a file upload and return it unmodified."""
    content = await read_upload_bytes(file, legacy=True)
    filename = file.filename or "output.pdf"
    media_type = file.content_type or "application/pdf"
    return file_response(content, media_type, filename, "output.pdf")
