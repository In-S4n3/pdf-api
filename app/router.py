"""API route registration.

Each tool gets its own endpoint (D-03). Tool routers are imported
and included here. The health endpoint is defined inline.
"""

import subprocess

from fastapi import APIRouter

from app.tools.compress import router as compress_router
from app.tools.echo import router as echo_router
from app.tools.flatten import router as flatten_router
from app.tools.pdfa import router as pdfa_router
from app.tools.redact import router as redact_router

router = APIRouter()
router.include_router(echo_router)
router.include_router(compress_router)
router.include_router(flatten_router)
router.include_router(pdfa_router)
router.include_router(redact_router)


@router.get("/health")
async def health():
    """Return service status and library versions (D-05)."""
    import pymupdf
    import pikepdf

    gs_result = subprocess.run(
        ["gs", "--version"], capture_output=True, text=True, timeout=10
    )
    tess_result = subprocess.run(
        ["tesseract", "--version"], capture_output=True, text=True, timeout=10
    )
    soffice_result = subprocess.run(
        ["soffice", "--headless", "--version"], capture_output=True, text=True, timeout=30
    )

    return {
        "status": "ok",
        "versions": {
            "pymupdf": pymupdf.VersionBind,
            "pikepdf": pikepdf.__version__,
            "ghostscript": gs_result.stdout.strip() if gs_result.returncode == 0 else "unavailable",
            "tesseract": tess_result.stdout.split("\n")[0] if tess_result.returncode == 0 else "unavailable",
            "libreoffice": soffice_result.stdout.strip() if soffice_result.returncode == 0 else "unavailable",
        },
    }
