"""Tests for POST /flatten endpoint."""

import io
from pathlib import Path

import pymupdf

ENCRYPTED_PDF = Path(__file__).parent / "fixtures" / "encrypted.pdf"


def _make_pdf_with_form_fields() -> bytes:
    """Create a PDF with a text widget for flatten testing."""
    doc = pymupdf.open()
    page = doc.new_page()
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "test_field"
    widget.field_value = "hello"
    widget.rect = pymupdf.Rect(50, 50, 200, 80)
    page.add_widget(widget)
    result = doc.tobytes()
    doc.close()
    return result


def _make_pdf_with_annotations() -> bytes:
    """Create a PDF with a highlight annotation for flatten testing."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.add_highlight_annot(pymupdf.Rect(50, 50, 200, 80))
    result = doc.tobytes()
    doc.close()
    return result


def test_flatten_returns_valid_pdf(client, sample_pdf):
    """Flatten endpoint returns a valid PDF."""
    response = client.post(
        "/flatten",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
    )
    assert response.status_code == 200
    doc = pymupdf.open(stream=response.content, filetype="pdf")
    assert len(doc) >= 1
    doc.close()


def test_flatten_removes_form_fields(client):
    """Flatten bakes form fields so the output has zero widgets."""
    pdf_bytes = _make_pdf_with_form_fields()

    # Verify the input actually has widgets
    in_doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    assert len(list(in_doc[0].widgets())) > 0
    in_doc.close()

    response = client.post(
        "/flatten",
        files={"file": ("form.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
    )
    assert response.status_code == 200

    out_doc = pymupdf.open(stream=response.content, filetype="pdf")
    assert len(list(out_doc[0].widgets())) == 0
    out_doc.close()


def test_flatten_removes_annotations(client):
    """Flatten bakes annotations so the output has zero annotations."""
    pdf_bytes = _make_pdf_with_annotations()

    # Verify the input actually has annotations
    in_doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    assert len(list(in_doc[0].annots())) > 0
    in_doc.close()

    response = client.post(
        "/flatten",
        files={"file": ("annot.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
    )
    assert response.status_code == 200

    out_doc = pymupdf.open(stream=response.content, filetype="pdf")
    annots = list(out_doc[0].annots()) if out_doc[0].annots() else []
    assert len(annots) == 0
    out_doc.close()


def test_flatten_preserves_filename(client, sample_pdf):
    """Flatten endpoint includes the original filename in Content-Disposition."""
    response = client.post(
        "/flatten",
        files={"file": ("my-doc.pdf", io.BytesIO(sample_pdf), "application/pdf")},
    )
    assert response.status_code == 200
    assert "my-doc.pdf" in response.headers.get("content-disposition", "")


def test_flatten_rejects_missing_file(client):
    """Flatten endpoint returns 422 when no file is provided."""
    response = client.post("/flatten")
    assert response.status_code == 422


def test_flatten_rejects_encrypted_pdf(client):
    """Encrypted PDFs return 400 with a Portuguese message (v1 envelope)."""
    response = client.post(
        "/flatten",
        files={"file": ("encrypted.pdf", io.BytesIO(ENCRYPTED_PDF.read_bytes()), "application/pdf")},
    )
    assert response.status_code == 400
    assert "palavra-passe" in response.json()["error"]


def test_v2_flatten_rejects_encrypted_pdf(client):
    """Encrypted PDFs return 400 + password_protected_pdf code (v2 envelope)."""
    response = client.post(
        "/v2/flatten",
        files={"file": ("encrypted.pdf", io.BytesIO(ENCRYPTED_PDF.read_bytes()), "application/pdf")},
        data={"options": "{}"},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "password_protected_pdf"
    assert "palavra-passe" in body["error"]["message"]
