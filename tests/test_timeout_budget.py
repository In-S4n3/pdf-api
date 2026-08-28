"""Cross-service timeout ordering used by TudoPDF's 60-second request path."""

from app.services.pdf_tools import (
    GS_REPAIR_TIMEOUT,
    REPAIR_WORKER_TIMEOUT,
    TOOL_SUBPROCESS_TIMEOUT,
)


def test_backend_finishes_before_the_50_second_tudopdf_proxy_deadline():
    assert TOOL_SUBPROCESS_TIMEOUT <= 45
    assert REPAIR_WORKER_TIMEOUT + GS_REPAIR_TIMEOUT <= 45
