"""Convert-to-PDF endpoint -- office docs via LibreOffice, images via img2pdf.

Accepts DOCX/XLSX/PPTX (OpenXML) and JPG/PNG/TIFF uploads. Routes to
LibreOffice subprocess for office documents and img2pdf for images.
Falls back to LibreOffice for images if img2pdf fails (e.g. CMYK JPEG).
"""

import subprocess
import tempfile
from pathlib import Path

import img2pdf
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key

router = APIRouter()

# OpenXML MIME types for office documents
OFFICE_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",       # .docx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",             # .xlsx
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",     # .pptx
}

# Image MIME types supported for conversion
IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/tiff",
}

# Map office MIME types to file extensions for LibreOffice
MIME_TO_EXT = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}

# Map image MIME types to file extensions (for LibreOffice fallback)
IMAGE_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
}


def _stem_from_filename(filename: str | None) -> str:
    """Extract stem (name without extension) from filename."""
    if not filename:
        return "output"
    return Path(filename).stem or "output"


async def _convert_office(content: bytes, content_type: str, filename: str | None) -> bytes:
    """Convert office document to PDF using LibreOffice subprocess."""
    ext = MIME_TO_EXT[content_type]

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / f"input{ext}"
        input_path.write_bytes(content)

        try:
            result = subprocess.run(
                [
                    "soffice",
                    "--headless",
                    "--norestore",
                    "--convert-to", "pdf",
                    "--outdir", tmpdir,
                    f"-env:UserInstallation=file://{tmpdir}/profile",
                    str(input_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            raise HTTPException(
                status_code=500,
                detail="Falha na conversao do documento",
            )

        output_path = Path(tmpdir) / "input.pdf"
        if result.returncode != 0 or not output_path.exists():
            raise HTTPException(
                status_code=500,
                detail="Falha na conversao do documento",
            )

        return output_path.read_bytes()


async def _convert_image(content: bytes, content_type: str) -> bytes:
    """Convert image to PDF using img2pdf, with LibreOffice fallback."""
    try:
        return img2pdf.convert(content)
    except Exception:
        # Fallback to LibreOffice for edge cases (e.g. CMYK JPEG)
        ext = IMAGE_MIME_TO_EXT.get(content_type, ".jpg")

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / f"input{ext}"
            input_path.write_bytes(content)

            try:
                result = subprocess.run(
                    [
                        "soffice",
                        "--headless",
                        "--norestore",
                        "--convert-to", "pdf",
                        "--outdir", tmpdir,
                        f"-env:UserInstallation=file://{tmpdir}/profile",
                        str(input_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except FileNotFoundError:
                raise HTTPException(
                    status_code=500,
                    detail="Falha na conversao da imagem",
                )

            output_path = Path(tmpdir) / "input.pdf"
            if result.returncode != 0 or not output_path.exists():
                raise HTTPException(
                    status_code=500,
                    detail="Falha na conversao da imagem",
                )

            return output_path.read_bytes()


@router.post("/convert")
async def convert(
    file: UploadFile = File(...),
    options: str = Form("{}"),
    _key: str = Depends(verify_api_key),
) -> Response:
    """Convert office documents or images to PDF."""
    content_type = file.content_type or ""
    content = await file.read()

    if content_type in OFFICE_MIMES:
        pdf_bytes = await _convert_office(content, content_type, file.filename)
    elif content_type in IMAGE_MIMES:
        pdf_bytes = await _convert_image(content, content_type)
    else:
        raise HTTPException(
            status_code=400,
            detail="Formato nao suportado",
        )

    stem = _stem_from_filename(file.filename)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{stem}.pdf"'},
    )
