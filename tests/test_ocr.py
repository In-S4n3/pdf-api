"""Tests for POST /ocr endpoint."""

import io
import json
import shutil

import pymupdf
import pytest
from PIL import Image, ImageDraw

from app.services import pdf_tools
from tests._env import can_ocr, tesseract_langs
from tests.test_audit_fixes import _scan

_NO_OCR = "requires full OCR toolchain (ocrmypdf + unpaper + tesseract pack; Docker-only)"


@pytest.mark.skipif(not can_ocr("eng"), reason=_NO_OCR)
def test_ocr_returns_valid_pdf(client):
    """OCR endpoint returns a valid PDF with default language (english)."""
    response = client.post(
        "/ocr",
        files={
            "file": ("test.pdf", io.BytesIO(_scan(["Annual accounts approved"])), "application/pdf")
        },
        data={"options": json.dumps({"language": "english"})},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


@pytest.mark.skipif(not can_ocr("por"), reason=_NO_OCR)
def test_ocr_accepts_portuguese(client):
    """OCR endpoint accepts portuguese language option."""
    response = client.post(
        "/ocr",
        files={"file": ("test.pdf", io.BytesIO(_scan(["Contas aprovadas"])), "application/pdf")},
        data={"options": json.dumps({"language": "portuguese"})},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


def test_ocr_rejects_invalid_language(client, sample_pdf):
    """OCR endpoint returns 400 for unsupported language."""
    response = client.post(
        "/ocr",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": json.dumps({"language": "klingon"})},
    )
    assert response.status_code == 400
    # API speaks Portuguese: "Idioma não suportado: <lang>. Suportados: ..."
    assert "não suportado" in response.json()["error"]


@pytest.mark.skipif(not can_ocr("eng"), reason=_NO_OCR)
def test_ocr_preserves_filename(client):
    """OCR endpoint includes original filename in Content-Disposition."""
    response = client.post(
        "/ocr",
        files={
            "file": ("scan.pdf", io.BytesIO(_scan(["Annual accounts approved"])), "application/pdf")
        },
        data={"options": json.dumps({"language": "english"})},
    )
    assert response.status_code == 200
    assert "scan.pdf" in response.headers.get("content-disposition", "")


def test_ocr_rejects_missing_file(client):
    """OCR endpoint returns 422 when no file is provided."""
    response = client.post("/ocr")
    assert response.status_code == 422


@pytest.mark.skipif(not can_ocr("jpn"), reason=_NO_OCR)
def test_ocr_accepts_jpn(client):
    """OCR endpoint accepts jpn language (validates passthrough in LANGUAGE_MAP)."""
    response = client.post(
        "/ocr",
        files={"file": ("test.pdf", io.BytesIO(_scan(["1234567890"])), "application/pdf")},
        data={"options": json.dumps({"language": "jpn"})},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


def _scanned_form(footer: str | None = "ScanApp") -> bytes:
    doc = pymupdf.open(stream=_scan(["Annual accounts approved"], footer=footer), filetype="pdf")
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "reviewer"
    widget.field_value = "Alice"
    widget.rect = pymupdf.Rect(50, 750, 200, 780)
    doc[0].add_widget(widget)
    result = doc.tobytes()
    doc.close()
    return result


def test_ocr_form_with_text_refuses_before_running_tool(client, monkeypatch):
    def no_tool(*_args, **_kwargs):
        raise AssertionError("ocrmypdf must not run on a fillable form")

    monkeypatch.setattr(pdf_tools, "_run_command", no_tool)
    form = _scanned_form()
    with pymupdf.open(stream=form, filetype="pdf") as doc:
        assert "ScanApp" in doc[0].get_text()
        assert "approved" not in doc[0].get_text().lower()
    response = client.post(
        "/v2/ocr",
        files={"file": ("form.pdf", io.BytesIO(form), "application/pdf")},
        data={"options": json.dumps({"language": "english"})},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "form_needs_flattening"
    assert "Achatar PDF" in response.json()["error"]["message"]


@pytest.mark.skipif(
    not (shutil.which("ocrmypdf") and shutil.which("gs") and "eng" in tesseract_langs()),
    reason="requires ocrmypdf, Ghostscript, and English Tesseract",
)
def test_flattened_form_scan_becomes_searchable():
    form = _scanned_form()
    flattened = pdf_tools.flatten_pdf(form)
    with pymupdf.open(stream=flattened, filetype="pdf") as doc:
        assert not doc.is_form_pdf
        assert "approved" not in doc[0].get_text().lower()

    result = pdf_tools.ocr_pdf(flattened, "english")
    with pymupdf.open(stream=result, filetype="pdf") as doc:
        assert "approved" in doc[0].get_text().lower()


@pytest.mark.skipif(
    not (shutil.which("ocrmypdf") and shutil.which("gs") and "eng" in tesseract_langs()),
    reason="requires ocrmypdf, Ghostscript, and English Tesseract",
)
def test_textless_scanned_form_uses_ocr_and_keeps_fields():
    form = _scanned_form(footer=None)
    with pymupdf.open(stream=form, filetype="pdf") as doc:
        assert doc.is_form_pdf
        assert doc[0].get_text().strip() == "Alice"  # widget text, not page content
        page_text = pymupdf.TextPage(doc[0].get_displaylist(annots=0).get_textpage())
        assert not page_text.extractText().strip()

    result = pdf_tools.ocr_pdf(form, "english")
    with pymupdf.open(stream=result, filetype="pdf") as doc:
        assert "approved" in doc[0].get_text().lower()
        assert doc.is_form_pdf


def _scan_with_second_page(kind: str) -> bytes:
    doc = pymupdf.open(stream=_scan(["Annual accounts approved"]), filetype="pdf")
    page = doc.new_page(width=595, height=842)
    if kind == "short_text":
        page.insert_text((72, 90), "Anexo A")
    else:
        image = Image.new("RGB", (1200, 1600), "white" if kind == "blank_scan" else "skyblue")
        if kind == "textless_photo":
            drawing = ImageDraw.Draw(image)
            drawing.rectangle((0, 1050, 1200, 1600), fill="forestgreen")
            drawing.ellipse((200, 150, 480, 430), fill="gold")
        buf = io.BytesIO()
        image.save(buf, format="JPEG")
        page.insert_image(page.rect, stream=buf.getvalue())
    result = doc.tobytes()
    doc.close()
    return result


@pytest.mark.skipif(
    not (shutil.which("ocrmypdf") and shutil.which("gs") and "eng" in tesseract_langs()),
    reason="requires ocrmypdf, Ghostscript, and English Tesseract",
)
@pytest.mark.parametrize("kind", ["blank_scan", "textless_photo", "short_text"])
def test_ocr_keeps_document_when_second_page_needs_no_new_text(client, kind):
    pdf = _scan_with_second_page(kind)
    response = client.post(
        "/v2/ocr",
        files={"file": ("two-pages.pdf", io.BytesIO(pdf), "application/pdf")},
        data={"options": json.dumps({"language": "english"})},
    )
    assert response.status_code == 200
    with pymupdf.open(stream=response.content, filetype="pdf") as doc:
        assert doc.page_count == 2
        assert "approved" in doc[0].get_text().lower()


@pytest.mark.skipif(
    not (shutil.which("ocrmypdf") and shutil.which("gs") and "eng" in tesseract_langs()),
    reason="requires ocrmypdf, Ghostscript, and English Tesseract",
)
def test_ocr_blank_only_refuses_with_honest_message(client):
    response = client.post(
        "/v2/ocr",
        files={"file": ("blank.pdf", io.BytesIO(_scan([])), "application/pdf")},
        data={"options": json.dumps({"language": "english"})},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ocr_no_text"
    assert "não encontrou texto para reconhecer" in response.json()["error"]["message"]


def test_ocr_refuses_page_with_no_recognised_text(client, monkeypatch):
    from types import SimpleNamespace

    scan = _scan(["Unrecognised pixels"], footer="ScanApp")
    with pymupdf.open(stream=scan, filetype="pdf") as doc:
        assert doc[0].get_text().strip() == "ScanApp"

    def unchanged(_command, **_kwargs):
        output = _command[-1]
        from pathlib import Path

        Path(output).write_bytes(scan)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(pdf_tools, "_run_command", unchanged)
    response = client.post(
        "/v2/ocr",
        files={"file": ("scan.pdf", io.BytesIO(scan), "application/pdf")},
        data={"options": json.dumps({"language": "english"})},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ocr_no_text"
    assert "não encontrou texto para reconhecer" in response.json()["error"]["message"]
