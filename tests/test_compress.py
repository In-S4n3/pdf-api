"""Tests for POST /compress endpoint."""

import io
from pathlib import Path

import pymupdf

ENCRYPTED_PDF = Path(__file__).parent / "fixtures" / "encrypted.pdf"


def _make_image_pdf() -> bytes:
    """Create a PDF with an embedded image for realistic compression testing."""
    doc = pymupdf.open()
    page = doc.new_page()
    # Create a large colored pixmap and insert as image
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 800, 600), 0)
    pix.set_rect(pix.irect, (200, 100, 50))  # fill with color (RGB, no alpha)
    img_bytes = pix.tobytes("png")
    page.insert_image(page.rect, stream=img_bytes)
    result = doc.tobytes()
    doc.close()
    return result


def test_compress_reduces_file_size(client):
    """Compressed output is smaller than input for image-heavy PDF."""
    pdf_bytes = _make_image_pdf()
    response = client.post(
        "/compress",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
    )
    assert response.status_code == 200
    assert len(response.content) < len(pdf_bytes)


def test_compress_returns_valid_pdf(client):
    """Compressed output is a valid PDF."""
    pdf_bytes = _make_image_pdf()
    response = client.post(
        "/compress",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
    )
    assert response.status_code == 200
    # Verify it opens as a valid PDF
    doc = pymupdf.open(stream=response.content, filetype="pdf")
    assert len(doc) >= 1
    doc.close()


def test_compress_handles_text_only_pdf(client, sample_pdf):
    """Text-only PDFs still get processed without error (deflate + garbage)."""
    response = client.post(
        "/compress",
        files={"file": ("text.pdf", io.BytesIO(sample_pdf), "application/pdf")},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_compress_preserves_filename(client, sample_pdf):
    """Compress endpoint includes the original filename in Content-Disposition."""
    response = client.post(
        "/compress",
        files={"file": ("my-doc.pdf", io.BytesIO(sample_pdf), "application/pdf")},
    )
    assert response.status_code == 200
    assert "my-doc.pdf" in response.headers.get("content-disposition", "")


def test_compress_rejects_missing_file(client):
    """Compress endpoint returns 422 when no file is provided."""
    response = client.post("/compress")
    assert response.status_code == 422


def test_compress_rejects_encrypted_pdf(client):
    """Encrypted PDFs return 400 with a Portuguese message (v1 envelope)."""
    response = client.post(
        "/compress",
        files={"file": ("encrypted.pdf", io.BytesIO(ENCRYPTED_PDF.read_bytes()), "application/pdf")},
    )
    assert response.status_code == 400
    assert "palavra-passe" in response.json()["error"]


def test_v2_compress_rejects_encrypted_pdf(client):
    """Encrypted PDFs return 400 + password_protected_pdf code (v2 envelope)."""
    response = client.post(
        "/v2/compress",
        files={"file": ("encrypted.pdf", io.BytesIO(ENCRYPTED_PDF.read_bytes()), "application/pdf")},
        data={"options": "{}"},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "password_protected_pdf"
    assert "palavra-passe" in body["error"]["message"]
