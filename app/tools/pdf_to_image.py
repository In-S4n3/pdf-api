"""PDF-to-Image endpoint -- stub for TDD RED phase."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key

router = APIRouter()


@router.post("/pdf-to-image")
async def pdf_to_image(
    file: UploadFile = File(...),
    options: str = Form("{}"),
    _key: str = Depends(verify_api_key),
) -> Response:
    """Stub -- not yet implemented."""
    raise HTTPException(status_code=501, detail="Not implemented")
