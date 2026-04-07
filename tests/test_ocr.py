"""Tests for POST /ocr endpoint."""

import io
import json
import shutil

import pytest

OCRMYPDF_AVAILABLE = shutil.which("ocrmypdf") is not None


@pytest.mark.skipif(not OCRMYPDF_AVAILABLE, reason="ocrmypdf CLI not installed")
def test_ocr_returns_valid_pdf(client, sample_pdf):
    """OCR endpoint returns a valid PDF with default language (english)."""
    response = client.post(
        "/ocr",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": json.dumps({"language": "english"})},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


@pytest.mark.skipif(not OCRMYPDF_AVAILABLE, reason="ocrmypdf CLI not installed")
def test_ocr_accepts_portuguese(client, sample_pdf):
    """OCR endpoint accepts portuguese language option."""
    response = client.post(
        "/ocr",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
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
    assert "Unsupported language" in response.json()["error"]


@pytest.mark.skipif(not OCRMYPDF_AVAILABLE, reason="ocrmypdf CLI not installed")
def test_ocr_preserves_filename(client, sample_pdf):
    """OCR endpoint includes original filename in Content-Disposition."""
    response = client.post(
        "/ocr",
        files={"file": ("scan.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": json.dumps({"language": "english"})},
    )
    assert response.status_code == 200
    assert "scan.pdf" in response.headers.get("content-disposition", "")


def test_ocr_rejects_missing_file(client):
    """OCR endpoint returns 422 when no file is provided."""
    response = client.post("/ocr")
    assert response.status_code == 422


@pytest.mark.skipif(not OCRMYPDF_AVAILABLE, reason="ocrmypdf CLI not installed")
def test_ocr_accepts_jpn(client, sample_pdf):
    """OCR endpoint accepts jpn language (validates passthrough in LANGUAGE_MAP)."""
    response = client.post(
        "/ocr",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": json.dumps({"language": "jpn"})},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"
