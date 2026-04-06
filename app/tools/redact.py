"""Redact endpoint -- permanently removes sensitive content from PDFs.

Uses PyMuPDF to search for text patterns (email, phone, custom text,
or user-provided regex) and apply true content-stream redaction.
Redacted text is permanently deleted, not just visually hidden.
"""

import json
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key

router = APIRouter()

# Built-in patterns for preset strategies
EMAIL_PATTERN = r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
PHONE_PATTERN = r"[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{7,15}"

PATTERNS = {
    "email": EMAIL_PATTERN,
    "phone": PHONE_PATTERN,
}

VALID_STRATEGIES = ("email", "phone", "custom", "regex")
MAX_REGEX_LENGTH = 500


@router.post("/redact")
async def redact(
    file: UploadFile = File(...),
    options: str = Form("{}"),
    _key: str = Depends(verify_api_key),
) -> Response:
    """Accept a PDF and return it with matched content permanently redacted."""
    import pymupdf

    opts = json.loads(options)
    strategy = opts.get("strategy", "email")
    custom_text = opts.get("customText", "")
    regex_pattern = opts.get("regexPattern", "")

    # -- Validate inputs --

    if strategy not in VALID_STRATEGIES:
        raise HTTPException(status_code=400, detail="Invalid redaction strategy")

    if strategy == "custom" and not custom_text.strip():
        raise HTTPException(status_code=400, detail="Custom text is required")

    if strategy == "regex":
        if not regex_pattern.strip():
            raise HTTPException(status_code=400, detail="Regex pattern is required")
        if len(regex_pattern) > MAX_REGEX_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"Regex pattern too long (max {MAX_REGEX_LENGTH} chars)",
            )
        try:
            re.compile(regex_pattern)
        except re.error:
            raise HTTPException(
                status_code=400,
                detail="Invalid regex pattern",
            )

    # -- Determine pattern and flags --

    if strategy in PATTERNS:
        pattern = PATTERNS[strategy]
        flags = 0
    elif strategy == "custom":
        pattern = re.escape(custom_text)
        flags = re.IGNORECASE
    else:  # regex
        pattern = regex_pattern
        flags = 0

    # -- Open document and redact --

    content = await file.read()
    doc = pymupdf.open(stream=content, filetype="pdf")

    # CRITICAL: Prevent adjacent line text deletion (pitfall 1)
    pymupdf.TOOLS.set_small_glyph_heights(True)

    for page in doc:
        text = page.get_text("text")
        matches = set()
        for m in re.finditer(pattern, text, flags):
            matches.add(m.group())

        page_has_redactions = False
        for match_text in matches:
            rects = page.search_for(match_text)
            for rect in rects:
                page.add_redact_annot(rect, fill=(0, 0, 0))
                page_has_redactions = True

        # Only apply redactions if annotations were added to this page
        if page_has_redactions:
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            )

    result = doc.tobytes(garbage=4, deflate=True)
    doc.close()

    filename = file.filename or "output.pdf"
    return Response(
        content=result,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
