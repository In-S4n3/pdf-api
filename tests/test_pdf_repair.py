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
    assert headers["X-Repair-Pages"] == "3/3"   # was "3" — M4: k/baseline when baseline known
    assert out[:5] == b"%PDF-"


def test_decompression_bomb_is_contained(monkeypatch):
    # The bomb decompresses to ~60MB. It is now expanded inside the Tier-1
    # subprocess worker, which is killed by REPAIR_WORKER_TIMEOUT (or, on Linux,
    # the RLIMIT_AS memory cap) before it can touch the API worker's memory.
    monkeypatch.setattr(pdf_tools, "REPAIR_WORKER_TIMEOUT", 3)
    with pytest.raises(ApiError) as e:
        repair_pdf(_b(REPAIR / "bomb.pdf"))
    assert e.value.status_code == 422
    assert e.value.code in {"repair_timeout", "repair_oom", "repair_too_large", "unrecoverable_pdf"}


import time, zlib


def _big_bomb(decoded_mb: int) -> bytes:
    huge = b"0 0 m\n" * (decoded_mb * 1024 * 1024 // 6)
    comp = zlib.compress(huge)
    return (
        b"%PDF-1.5\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>endobj\n"
        b"4 0 obj<</Length " + str(len(comp)).encode() + b"/Filter/FlateDecode>>stream\n"
        + comp + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF"
    )


def test_big_bomb_is_contained_in_subprocess_fast(monkeypatch):
    # 400MB decoded: in-process Tier-1 would run ~140s; the killable worker is
    # cut off at the short timeout. Fast 422 == the expansion happened in the
    # subprocess, not the API worker (the C1 fix).
    monkeypatch.setattr(pdf_tools, "REPAIR_WORKER_TIMEOUT", 3)
    t0 = time.time()
    with pytest.raises(ApiError) as e:
        repair_pdf(_big_bomb(400))
    elapsed = time.time() - t0
    assert e.value.status_code == 422
    assert elapsed < 12, f"took {elapsed:.1f}s — bomb was NOT contained in a killable subprocess"


def test_endpoint_repairs_and_sets_headers(client):
    resp = client.post(
        "/v2/pdf-repair",
        files={"file": ("broken.pdf", io.BytesIO((REPAIR / "broken_xref.pdf").read_bytes()), "application/pdf")},
    )
    assert resp.status_code == 200
    assert resp.headers["X-Repair-Status"] == "repaired"
    assert resp.headers["X-Repair-Method"] == "pikepdf"
    assert resp.content[:5] == b"%PDF-"


def test_endpoint_encrypted_returns_400(client):
    resp = client.post(
        "/v2/pdf-repair",
        files={"file": ("enc.pdf", io.BytesIO((FIX / "encrypted.pdf").read_bytes()), "application/pdf")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "password_protected"
