"""API route registration.

Each tool gets its own endpoint (D-03). Tool routers are imported
and included here. The health endpoint is defined inline.
"""

from fastapi import APIRouter

from app.http_utils import run_service
from app.router_v2 import router as v2_router
from app.services.pdf_tools import build_health_payload
from app.tools.compress import router as compress_router
from app.tools.convert import router as convert_router
from app.tools.echo import router as echo_router
from app.tools.fill_form import router as fill_form_router
from app.tools.flatten import router as flatten_router
from app.tools.ocr import router as ocr_router
from app.tools.pdf_to_image import router as pdf_to_image_router
from app.tools.pdfa import router as pdfa_router
from app.tools.protect import router as protect_router
from app.tools.redact import router as redact_router

router = APIRouter()
router.include_router(echo_router)
router.include_router(compress_router)
router.include_router(convert_router)
router.include_router(flatten_router)
router.include_router(ocr_router)
router.include_router(pdfa_router)
router.include_router(redact_router)
router.include_router(fill_form_router)
router.include_router(pdf_to_image_router)
router.include_router(protect_router)
router.include_router(v2_router)


@router.get("/health")
async def health():
    """Return service status and library versions (D-05)."""
    return await run_service(build_health_payload)
