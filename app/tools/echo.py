"""Echo endpoint -- returns the uploaded file unmodified.

Used to validate the multipart upload / binary response contract
without any PDF processing. Critical for Phase 9-10 integration testing.
"""

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import Response

router = APIRouter()


@router.post("/echo")
async def echo(file: UploadFile = File(...)) -> Response:
    """Accept a file upload and return it unmodified."""
    content = await file.read()
    filename = file.filename or "output.pdf"
    media_type = file.content_type or "application/pdf"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
