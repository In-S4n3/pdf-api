"""PDF/A conversion endpoint -- converts PDF to archival PDF/A format.

Uses OCRmyPDF CLI (subprocess) with --skip-text to convert without
performing OCR. Supports PDF/A-1b, 2b (default), and 3b conformance.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key

router = APIRouter()

# Map TudoPDF frontend values to OCRmyPDF --output-type values.
# Frontend sends "pdfa-2b", OCRmyPDF expects "pdfa-2".
CONFORMANCE_MAP = {
    "pdfa-1b": "pdfa-1",
    "pdfa-2b": "pdfa-2",
    "pdfa-3b": "pdfa-3",
}


@router.post("/pdfa")
async def pdfa(
    file: UploadFile = File(...),
    options: str = Form("{}"),
    _key: str = Depends(verify_api_key),
) -> Response:
    """Accept a PDF and return a PDF/A-compliant version."""
    opts = json.loads(options)
    conformance = opts.get("conformance", "pdfa-2b")
    output_type = CONFORMANCE_MAP.get(conformance)

    if output_type is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid conformance level: {conformance}. "
            f"Supported: {', '.join(CONFORMANCE_MAP.keys())}",
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
                "--output-type", output_type,
                "--optimize", "1",
                str(input_path),
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail="PDF/A conversion failed",
            )

        pdf_bytes = output_path.read_bytes()

    filename = file.filename or "output.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
