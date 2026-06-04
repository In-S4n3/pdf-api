"""Tests for POST /pdf-to-image endpoint."""

import io
import json
import zipfile

import pymupdf


def _make_test_pdf(pages: int = 1) -> bytes:
    """Create a simple test PDF with text content."""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Pagina {i + 1} de teste", fontsize=12)
    result = doc.tobytes()
    doc.close()
    return result


# -- Happy path --


def test_pdf_to_image_png_returns_200(client):
    """POST /pdf-to-image with valid PDF + format=png returns 200 with image/png."""
    pdf_bytes = _make_test_pdf()
    response = client.post(
        "/pdf-to-image",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"format": "png"})},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_pdf_to_image_jpeg_returns_200(client):
    """POST /pdf-to-image with valid PDF + format=jpeg returns 200 with image/jpeg."""
    pdf_bytes = _make_test_pdf()
    response = client.post(
        "/pdf-to-image",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"format": "jpeg"})},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"


def test_png_magic_bytes(client):
    """PNG response body starts with PNG magic bytes (b'\\x89PNG')."""
    pdf_bytes = _make_test_pdf()
    response = client.post(
        "/pdf-to-image",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"format": "png"})},
    )
    assert response.status_code == 200
    assert response.content[:4] == b"\x89PNG"


def test_jpeg_magic_bytes(client):
    """JPEG response body starts with JPEG magic bytes (b'\\xff\\xd8')."""
    pdf_bytes = _make_test_pdf()
    response = client.post(
        "/pdf-to-image",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"format": "jpeg"})},
    )
    assert response.status_code == 200
    assert response.content[:2] == b"\xff\xd8"


def test_default_format_returns_png(client):
    """Default format (no format option) returns PNG."""
    pdf_bytes = _make_test_pdf()
    response = client.post(
        "/pdf-to-image",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({})},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:4] == b"\x89PNG"


def test_invalid_format_returns_400(client):
    """Invalid format (format=webp) returns 400."""
    pdf_bytes = _make_test_pdf()
    response = client.post(
        "/pdf-to-image",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"format": "webp"})},
    )
    assert response.status_code == 400


def test_invalid_pdf_returns_400_portuguese(client):
    """Invalid PDF content (random bytes) returns 400 with Portuguese error message."""
    response = client.post(
        "/pdf-to-image",
        files={"file": ("bad.pdf", io.BytesIO(b"not a real pdf"), "application/pdf")},
        data={"options": json.dumps({"format": "png"})},
    )
    assert response.status_code == 400
    body = response.json()
    assert "possivel" in body["error"].lower() or "valido" in body["error"].lower()


def test_missing_file_returns_422(client):
    """POST /pdf-to-image without file returns 422 (FastAPI validation)."""
    response = client.post(
        "/pdf-to-image",
        data={"options": json.dumps({"format": "png"})},
    )
    assert response.status_code == 422


def test_content_disposition_header(client):
    """Content-Disposition header contains the original filename with correct extension."""
    pdf_bytes = _make_test_pdf()
    # Test PNG extension
    response = client.post(
        "/pdf-to-image",
        files={"file": ("relatorio.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"format": "png"})},
    )
    assert response.status_code == 200
    assert "relatorio.png" in response.headers.get("content-disposition", "")

    # Test JPEG extension
    response = client.post(
        "/pdf-to-image",
        files={"file": ("relatorio.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"format": "jpeg"})},
    )
    assert response.status_code == 200
    assert "relatorio.jpg" in response.headers.get("content-disposition", "")


def test_png_output_has_reasonable_size(client):
    """PNG output has reasonable size (> 1000 bytes for a page with text)."""
    pdf_bytes = _make_test_pdf()
    response = client.post(
        "/pdf-to-image",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"format": "png"})},
    )
    assert response.status_code == 200
    assert len(response.content) > 1000


# -- Multi-page (all pages → ZIP) --


def test_all_pages_returns_zip(client):
    """pages=all returns a ZIP archive with one image per page."""
    pdf_bytes = _make_test_pdf(pages=3)
    response = client.post(
        "/v2/pdf-to-image",
        files={"file": ("doc.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"format": "png", "pages": "all"})},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    assert len(zf.namelist()) == 3
    assert "pagina-1.png" in zf.namelist()
    assert "pagina-3.png" in zf.namelist()
    for name in zf.namelist():
        assert zf.read(name)[:4] == b"\x89PNG"


def test_all_pages_jpeg_format(client):
    """pages=all with format=jpeg returns ZIP of JPEGs."""
    pdf_bytes = _make_test_pdf(pages=2)
    response = client.post(
        "/v2/pdf-to-image",
        files={"file": ("doc.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"format": "jpeg", "pages": "all"})},
    )
    assert response.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    assert len(zf.namelist()) == 2
    assert "pagina-1.jpg" in zf.namelist()
    for name in zf.namelist():
        assert zf.read(name)[:2] == b"\xff\xd8"


def test_all_pages_zip_content_disposition(client):
    """ZIP response has correct Content-Disposition filename."""
    pdf_bytes = _make_test_pdf(pages=2)
    response = client.post(
        "/v2/pdf-to-image",
        files={"file": ("relatorio.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"pages": "all"})},
    )
    assert response.status_code == 200
    assert "relatorio-imagens.zip" in response.headers.get("content-disposition", "")


def test_too_many_pages_returns_400(client):
    """PDF with >20 pages and pages=all returns 400 too_many_pages."""
    pdf_bytes = _make_test_pdf(pages=21)
    response = client.post(
        "/v2/pdf-to-image",
        files={"file": ("big.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"pages": "all"})},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "too_many_pages"


def test_first_page_backward_compat(client):
    """pages=first (default) still returns single image, not ZIP."""
    pdf_bytes = _make_test_pdf(pages=5)
    response = client.post(
        "/v2/pdf-to-image",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"format": "png", "pages": "first"})},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:4] == b"\x89PNG"
