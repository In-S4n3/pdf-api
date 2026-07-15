# tests/test_pdf_repair.py
import io
from pathlib import Path

import pytest

from app.api_errors import ApiError
from app.services.pdf_tools import repair_pdf

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
