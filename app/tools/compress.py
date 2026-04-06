"""Compress endpoint -- reduces PDF file size using PyMuPDF.

Applies image rewriting (downsampling + JPEG recompression),
garbage collection, stream deflation, and object stream compression.
"""

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key

router = APIRouter()


@router.post("/compress")
async def compress(
    file: UploadFile = File(...),
    _key: str = Depends(verify_api_key),
) -> Response:
    """Accept a PDF and return a compressed version."""
    import pymupdf

    content = await file.read()
    doc = pymupdf.open(stream=content, filetype="pdf")

    # Image rewriting: downsample anything above 150 DPI to 96 DPI at JPEG Q75
    # This is the biggest contributor to size reduction (70-90% for image-heavy PDFs)
    doc.rewrite_images(
        dpi_threshold=150,
        dpi_target=96,
        quality=75,
    )

    # Save with maximum cleanup:
    # - garbage=4: remove unused objects + deduplicate streams (most thorough)
    # - deflate=True: zlib-compress all uncompressed streams
    # - clean=True: sanitize content streams
    # - use_objstms=True: pack text objects into compressible streams (~25% extra)
    result = doc.tobytes(
        garbage=4,
        deflate=True,
        clean=True,
        use_objstms=True,
    )
    doc.close()

    filename = file.filename or "output.pdf"
    return Response(
        content=result,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
