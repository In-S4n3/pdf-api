"""Tests for POST /convert endpoint."""

import io
import shutil
import zipfile

import pytest
from PIL import Image

SOFFICE_AVAILABLE = shutil.which("soffice") is not None


def _minimal_docx() -> bytes:
    """Create a minimal valid DOCX file (ZIP with required XML structure)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType='
            '"application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type='
            '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
            ' Target="word/document.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Test</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )
    return buf.getvalue()


def _minimal_xlsx() -> bytes:
    """Create a minimal valid XLSX file (ZIP with required XML structure)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType='
            '"application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type='
            '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
            ' Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheets><sheet name=\"Sheet1\" sheetId=\"1\" r:id=\"rId1\""
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
            "</sheets></workbook>",
        )
    return buf.getvalue()


def _minimal_pptx() -> bytes:
    """Create a minimal valid PPTX file (ZIP with required XML structure)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType='
            '"application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/ppt/presentation.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type='
            '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
            ' Target="ppt/presentation.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "ppt/presentation.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "</p:presentation>",
        )
    return buf.getvalue()


def _minimal_jpeg() -> bytes:
    """Create a minimal 10x10 JPEG image using Pillow."""
    buf = io.BytesIO()
    img = Image.new("RGB", (10, 10), color=(255, 0, 0))
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _minimal_png() -> bytes:
    """Create a minimal 10x10 PNG image using Pillow."""
    buf = io.BytesIO()
    img = Image.new("RGB", (10, 10), color=(0, 255, 0))
    img.save(buf, format="PNG")
    return buf.getvalue()


# -- Office conversion tests (require LibreOffice) --


@pytest.mark.skipif(not SOFFICE_AVAILABLE, reason="soffice (LibreOffice) not installed")
def test_convert_docx_returns_pdf(client):
    """DOCX file is converted to a valid PDF."""
    docx = _minimal_docx()
    response = client.post(
        "/convert",
        files={
            "file": (
                "document.docx",
                io.BytesIO(docx),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={"options": "{}"},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


@pytest.mark.skipif(not SOFFICE_AVAILABLE, reason="soffice (LibreOffice) not installed")
def test_convert_xlsx_returns_pdf(client):
    """XLSX file is converted to a valid PDF."""
    xlsx = _minimal_xlsx()
    response = client.post(
        "/convert",
        files={
            "file": (
                "spreadsheet.xlsx",
                io.BytesIO(xlsx),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"options": "{}"},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


@pytest.mark.skipif(not SOFFICE_AVAILABLE, reason="soffice (LibreOffice) not installed")
def test_convert_pptx_returns_pdf(client):
    """PPTX file is converted to a valid PDF."""
    pptx = _minimal_pptx()
    response = client.post(
        "/convert",
        files={
            "file": (
                "presentation.pptx",
                io.BytesIO(pptx),
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
        },
        data={"options": "{}"},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


# -- Image conversion tests (img2pdf, no soffice needed) --


def test_convert_jpg_returns_pdf(client):
    """JPEG image is converted to a valid PDF via img2pdf."""
    jpg = _minimal_jpeg()
    response = client.post(
        "/convert",
        files={"file": ("photo.jpg", io.BytesIO(jpg), "image/jpeg")},
        data={"options": "{}"},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


def test_convert_png_returns_pdf(client):
    """PNG image is converted to a valid PDF via img2pdf."""
    png = _minimal_png()
    response = client.post(
        "/convert",
        files={"file": ("diagram.png", io.BytesIO(png), "image/png")},
        data={"options": "{}"},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


# -- Validation tests --


def test_convert_rejects_unsupported_type(client):
    """Unsupported MIME type returns 400 with Portuguese error message."""
    response = client.post(
        "/convert",
        files={"file": ("readme.txt", io.BytesIO(b"hello world"), "text/plain")},
        data={"options": "{}"},
    )
    assert response.status_code == 400
    assert "Formato nao suportado" in response.json()["error"]


def test_convert_rejects_missing_file(client):
    """Missing file returns 422."""
    response = client.post("/convert")
    assert response.status_code == 422


def test_convert_preserves_filename(client):
    """Output filename preserves original stem with .pdf extension."""
    jpg = _minimal_jpeg()
    response = client.post(
        "/convert",
        files={"file": ("photo.jpg", io.BytesIO(jpg), "image/jpeg")},
        data={"options": "{}"},
    )
    assert response.status_code == 200
    disposition = response.headers.get("content-disposition", "")
    assert "photo.pdf" in disposition
