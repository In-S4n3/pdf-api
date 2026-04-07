"""PDF-to-Image endpoint -- renders first page as PNG or JPEG at 300 DPI.

Uses PyMuPDF page.get_pixmap(dpi=300) for high-quality rendering
with native PNG/JPEG output via tobytes().
"""

import json

import pymupdf
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
    """Accept a PDF and return the first page as an image."""
    opts = json.loads(options)
    fmt = opts.get("format", "png")
    if fmt not in ("png", "jpeg"):
        raise HTTPException(status_code=400, detail="Format must be 'png' or 'jpeg'")

    content = await file.read()

    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Nao foi possivel abrir o PDF. Verifique se o ficheiro e valido.",
        )

    try:
        page = doc[0]
        pix = page.get_pixmap(dpi=300)

        if fmt == "jpeg":
            result = pix.tobytes("jpeg", jpg_quality=92)
            media_type = "image/jpeg"
            ext = "jpg"
        else:
            result = pix.tobytes("png")
            media_type = "image/png"
            ext = "png"

        pix = None  # Release pixmap memory
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Nao foi possivel converter este PDF para imagem.",
        )
    finally:
        doc.close()

    base = (file.filename or "output.pdf").rsplit(".", 1)[0]
    return Response(
        content=result,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{base}.{ext}"'},
    )
