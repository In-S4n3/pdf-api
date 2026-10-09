"""Cross-service timeout ordering on TudoPDF's request path: 60 s for every tool,
300 s (Vercel's ceiling) for OCR alone."""

import inspect
import re
from pathlib import Path

from app.services import pdf_tools
from app.services.pdf_tools import (
    GS_REPAIR_TIMEOUT,
    OCR_SUBPROCESS_TIMEOUT,
    REPAIR_WORKER_TIMEOUT,
    TOOL_SUBPROCESS_TIMEOUT,
)

ROOT = Path(__file__).resolve().parent.parent


def test_backend_finishes_before_the_50_second_tudopdf_proxy_deadline():
    assert TOOL_SUBPROCESS_TIMEOUT <= 45
    assert REPAIR_WORKER_TIMEOUT + GS_REPAIR_TIMEOUT <= 45


def test_only_ocr_gets_the_long_budget():
    """Every other tool's caller still gives up at ~55 s."""
    assert inspect.getsource(pdf_tools).count("timeout=OCR_SUBPROCESS_TIMEOUT") == 1
    assert "timeout=OCR_SUBPROCESS_TIMEOUT" in inspect.getsource(pdf_tools.ocr_pdf)


def test_cloud_run_outlasts_ocr_and_answers_before_vercel():
    """Cloud Run cuts a request at timeoutSeconds: under the OCR kill plus cold
    start (~25 s) and the checks, it drops the job before our typed 504; at
    Vercel's 300 s, TudoPDF hears nothing."""
    service = re.findall(r"timeoutSeconds: (\d+)", (ROOT / "service.yaml").read_text())
    deploy = re.findall(
        r"\btimeout: (\d+)s\b", (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    )
    assert service == deploy and len(service) == 1
    assert OCR_SUBPROCESS_TIMEOUT + 60 <= int(service[0]) < 300
