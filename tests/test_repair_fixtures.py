# tests/test_repair_fixtures.py
import io
from pathlib import Path

import pikepdf
import pytest

REPAIR = Path(__file__).parent / "fixtures" / "repair"


def _bytes(name: str) -> bytes:
    return (REPAIR / name).read_bytes()


def test_healthy_opens_clean():
    pdf = pikepdf.open(io.BytesIO(_bytes("healthy.pdf")))
    assert len(pdf.check_pdf_syntax()) == 0
    assert len(pdf.pages) == 3
    pdf.close()


def test_broken_xref_still_recoverable_by_pikepdf():
    # qpdf reconstructs the xref -> opens, and after save the check is clean
    pdf = pikepdf.open(io.BytesIO(_bytes("broken_xref.pdf")))
    assert len(pdf.pages) == 3
    pdf.close()


def test_bomb_is_small_on_disk():
    assert len(_bytes("bomb.pdf")) < 1 * 1024 * 1024  # tiny file, huge decoded
