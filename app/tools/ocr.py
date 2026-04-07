"""OCR endpoint -- adds searchable text layer to scanned PDFs.

Uses OCRmyPDF CLI (subprocess) with --skip-text to OCR pages
that lack a text layer. Supports 8 languages via Tesseract.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key

router = APIRouter()

LANGUAGE_MAP = {
    "english": "eng",
    "spanish": "spa",
    "french": "fra",
    "german": "deu",
    "portuguese": "por",
    "italian": "ita",
    "chinese": "chi_sim",
    "jpn": "jpn",
}


@router.post("/ocr")
async def ocr(
    file: UploadFile = File(...),
    options: str = Form("{}"),
    _key: str = Depends(verify_api_key),
) -> Response:
    """Accept a PDF and return it with an OCR text layer."""
    opts = json.loads(options)
    language = opts.get("language", "english")
    lang_code = LANGUAGE_MAP.get(language)

    if lang_code is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language: {language}. "
            f"Supported: {', '.join(LANGUAGE_MAP.keys())}",
        )

    content = await file.read()

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.pdf"
        output_path = Path(tmpdir) / "output.pdf"
        input_path.write_bytes(content)

        result = subprocess.run(
            [
                "ocrmypdf",
                "--skip-text",
                "--output-type", "pdf",
                "-l", lang_code,
                "--deskew",
                "--clean",
                "--optimize", "1",
                "--tesseract-timeout", "60",
                str(input_path),
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode == 2:
            raise HTTPException(
                status_code=400,
                detail="Ficheiro PDF invalido ou corrompido",
            )

        if result.returncode == 8:
            raise HTTPException(
                status_code=400,
                detail="PDF protegido por palavra-passe. Remova a protecao primeiro.",
            )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail="Falha no processamento OCR",
            )

        pdf_bytes = output_path.read_bytes()

    filename = file.filename or "output.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
