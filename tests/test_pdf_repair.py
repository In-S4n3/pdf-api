# tests/test_pdf_repair.py
import io
from pathlib import Path

import pytest

from app.api_errors import ApiError
from app.services.pdf_tools import repair_pdf
import app.services.pdf_tools as pdf_tools  # for monkeypatching GS_REPAIR_TIMEOUT

REPAIR = Path(__file__).parent / "fixtures" / "repair"
FIX = Path(__file__).parent / "fixtures"


def _b(p: Path) -> bytes:
    return p.read_bytes()


def test_healthy_pdf_is_already_healthy():
    out, headers = repair_pdf(_b(REPAIR / "healthy.pdf"))
    assert headers["X-Repair-Status"] == "already-healthy"
    assert headers["X-Repair-Method"] == "pikepdf"
    assert out[:5] == b"%PDF-"


def test_broken_xref_is_repaired():
    out, headers = repair_pdf(_b(REPAIR / "broken_xref.pdf"))
    assert headers["X-Repair-Status"] == "repaired"
    assert headers["X-Repair-Pages"] == "3/3"


def test_non_pdf_bytes_rejected():
    with pytest.raises(ApiError) as e:
        repair_pdf(b"this is not a pdf at all")
    assert e.value.status_code == 400
    assert e.value.code == "not_a_pdf"


def test_encrypted_pdf_steers_to_unlock():
    with pytest.raises(ApiError) as e:
        repair_pdf(_b(FIX / "encrypted.pdf"))
    assert e.value.status_code == 400
    assert e.value.code == "password_protected"


def test_truncated_escalates_to_ghostscript():
    # truncated.pdf: pikepdf opens it but its save() output stays dirty ->
    # repair_pdf escalates to Ghostscript, which re-interprets it (lossy).
    out, headers = repair_pdf(_b(REPAIR / "truncated.pdf"))
    assert headers["X-Repair-Status"] == "reinterpreted-lossy"
    assert headers["X-Repair-Method"] == "ghostscript"
    assert headers["X-Repair-Pages"] == "3"
    assert out[:5] == b"%PDF-"


def test_decompression_bomb_is_contained(monkeypatch):
    # The bomb decompresses to ~60MB. On Linux prod the RLIMIT_AS memory cap
    # kills the gs child (oom); on any platform the timeout also bounds it.
    # Monkeypatch the timeout down so the suite stays fast, and accept any
    # clean containment code — the point is: the request fails with a 422 and
    # the pytest process survives (no OOM-kill of the runner).
    monkeypatch.setattr(pdf_tools, "GS_REPAIR_TIMEOUT", 5)
    with pytest.raises(ApiError) as e:
        repair_pdf(_b(REPAIR / "bomb.pdf"))
    assert e.value.status_code == 422
    assert e.value.code in {"repair_timeout", "repair_oom", "repair_too_large", "unrecoverable_pdf"}
