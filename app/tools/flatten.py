"""Flatten endpoint -- bakes annotations and form fields into permanent page content.

Uses PyMuPDF bake() to convert all annotations (highlights, comments,
stamps, ink, etc.) and form widgets (text fields, checkboxes, radio
buttons, dropdowns) into static page content.
"""

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key

router = APIRouter()


@router.post("/flatten")
async def flatten(
    file: UploadFile = File(...),
    _key: str = Depends(verify_api_key),
) -> Response:
    """Accept a PDF and return it with annotations/forms baked in."""
    import pymupdf

    content = await file.read()
    doc = pymupdf.open(stream=content, filetype="pdf")

    # Bake ALL annotations and form fields into permanent page content.
    # annots=True: highlights, comments, stamps, ink drawings, etc.
    # widgets=True: text fields, checkboxes, radio buttons, dropdowns.
    doc.bake(annots=True, widgets=True)

    # Save with cleanup (remove orphaned objects from baked annotations)
    result = doc.tobytes(garbage=4, deflate=True)
    doc.close()

    filename = file.filename or "output.pdf"
    return Response(
        content=result,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
