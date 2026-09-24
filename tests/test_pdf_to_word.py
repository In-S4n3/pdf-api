"""Tests for the pdf_to_docx service (PDF -> editable .docx)."""

import io
import zipfile
from pathlib import Path

import pymupdf
import pytest

from app.api_errors import ApiError
from app.services.pdf_tools import _docx_is_effectively_empty, pdf_to_docx


def _text_pdf(pages: int = 1, text: str = "Contrato de teste 12345") -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=14)
    out = doc.tobytes()
    doc.close()
    return out


def _blank_pdf(pages: int = 1) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()  # no text -> simulates a scanned/image-only page
    out = doc.tobytes()
    doc.close()
    return out


def _encrypted_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "segredo", fontsize=12)
    out = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    doc.close()
    return out


def test_text_pdf_returns_valid_docx():
    result = pdf_to_docx(_text_pdf(text="Contrato de teste 12345"))
    assert isinstance(result, bytes) and len(result) > 0
    with zipfile.ZipFile(io.BytesIO(result)) as zf:
        names = zf.namelist()
        assert "word/document.xml" in names
        document_xml = zf.read("word/document.xml").decode("utf-8", "replace")
    assert "12345" in document_xml


def test_scanned_pdf_raises_422():
    with pytest.raises(ApiError) as exc:
        pdf_to_docx(_blank_pdf())
    assert exc.value.status_code == 422
    assert exc.value.code == "scanned_pdf"


def test_encrypted_pdf_raises_400():
    with pytest.raises(ApiError) as exc:
        pdf_to_docx(_encrypted_pdf())
    assert exc.value.status_code == 400
    assert exc.value.code == "password_protected_pdf"


def test_corrupt_bytes_raise_400():
    with pytest.raises(ApiError) as exc:
        pdf_to_docx(b"this is not a pdf")
    assert exc.value.status_code == 400
    assert exc.value.code == "invalid_pdf"


def test_empty_docx_helper(tmp_path: Path):
    import docx

    blank = tmp_path / "blank.docx"
    docx.Document().save(blank)
    assert _docx_is_effectively_empty(blank) is True

    filled = tmp_path / "filled.docx"
    d = docx.Document()
    d.add_paragraph("Olá mundo")
    d.save(filled)
    assert _docx_is_effectively_empty(filled) is False


def test_v2_pdf_to_word_text_returns_200(client):
    pdf_bytes = io.BytesIO(_text_pdf(text="Relatorio 98765"))
    response = client.post(
        "/v2/pdf-to-word",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
        data={"options": "{}"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    # Name the parameter, not the tail of the string: RFC 6266 Appendix D
    # puts the ASCII `filename` first, so `filename*` is what ends the value.
    assert 'filename="test.docx"' in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert "word/document.xml" in zf.namelist()


def test_v2_pdf_to_word_scanned_returns_422_envelope(client):
    response = client.post(
        "/v2/pdf-to-word",
        files={"file": ("scan.pdf", io.BytesIO(_blank_pdf()), "application/pdf")},
        data={"options": "{}"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "scanned_pdf"


def test_v2_pdf_to_word_encrypted_returns_400_envelope(client):
    response = client.post(
        "/v2/pdf-to-word",
        files={"file": ("enc.pdf", io.BytesIO(_encrypted_pdf()), "application/pdf")},
        data={"options": "{}"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "password_protected_pdf"


def _vector_pdf(pages: int, items_per_page: int) -> bytes:
    """A PDF whose weight is vector paths, not pages or text.

    `pdf2docx` walks every path looking for table borders, so this is the shape
    that blows the subprocess timeout while looking small on disk.
    """
    import random

    random.seed(7)
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        shape = page.new_shape()
        for _ in range(items_per_page):
            x0, y0 = random.uniform(40, 500), random.uniform(40, 740)
            shape.draw_line((x0, y0), (x0 + random.uniform(5, 60), y0 + random.uniform(-20, 20)))
        shape.finish(width=0.4, color=(0, 0, 0))
        shape.commit()
        page.insert_textbox(
            pymupdf.Rect(50, 50, 545, 300), "Relatorio financeiro " * 60, fontsize=9
        )
    out = doc.tobytes(deflate=True)
    doc.close()
    return out


def test_vector_heavy_pdf_raises_422_instead_of_timing_out():
    """The shape that produced four 504s in production on 2026-08-13.

    A 1.4 MB PDF took over 45s in `pdf2docx` and the user retried four times in
    five minutes. Measured locally: 30 000 vector items convert in 10s and
    50 000 in 31s, on hardware faster than the 2-vCPU container. Rejecting is
    the honest answer; timing out is not.
    """
    with pytest.raises(ApiError) as exc:
        pdf_to_docx(_vector_pdf(pages=10, items_per_page=5000))
    assert exc.value.status_code == 422
    assert exc.value.code == "pdf_too_complex"


def test_moderate_vector_pdf_still_converts():
    """The gate must not reject an ordinary PDF that happens to have lines."""
    result = pdf_to_docx(_vector_pdf(pages=3, items_per_page=200))
    assert isinstance(result, bytes) and len(result) > 0


def test_many_pages_of_plain_text_are_not_rejected():
    """The cost driver is paths, not size or pages — 200 text pages convert fine.

    Pins the gate to the dimension that was actually measured: this file is
    larger than the one that timed out and carries zero vector items.
    """
    result = pdf_to_docx(_text_pdf(pages=180, text="Contrato de teste 12345 " * 40))
    assert isinstance(result, bytes) and len(result) > 0


def test_default_max_vector_items_is_the_measured_ceiling():
    from app.config import DEFAULT_MAX_VECTOR_ITEMS, get_settings

    assert get_settings().max_vector_items == DEFAULT_MAX_VECTOR_ITEMS == 30_000


def test_max_vector_items_env_var_tunes_the_gate(monkeypatch):
    """The knob has to work: the one number the curve cannot supply is how much
    slower the 2-vCPU container is than the machine it was measured on.

    Same file, both sides of the threshold — so the gate is shown able to say
    both yes and no, not merely to agree with itself.
    """
    ordinary = _vector_pdf(pages=3, items_per_page=200)  # 600 items

    monkeypatch.setenv("MAX_VECTOR_ITEMS", "100")
    with pytest.raises(ApiError) as exc:
        pdf_to_docx(ordinary)
    assert exc.value.status_code == 422
    assert exc.value.code == "pdf_too_complex"

    monkeypatch.setenv("MAX_VECTOR_ITEMS", "10000")
    assert len(pdf_to_docx(ordinary)) > 0
