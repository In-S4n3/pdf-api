"""Tests for POST /pdfa endpoint."""

import io
import json
import shutil

import pytest

OCRMYPDF_AVAILABLE = shutil.which("ocrmypdf") is not None


@pytest.mark.skipif(not OCRMYPDF_AVAILABLE, reason="ocrmypdf CLI not installed")
def test_pdfa_returns_valid_pdf(client, sample_pdf):
    """PDF/A endpoint returns a valid PDF with default conformance."""
    response = client.post(
        "/pdfa",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": json.dumps({})},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


@pytest.mark.skipif(not OCRMYPDF_AVAILABLE, reason="ocrmypdf CLI not installed")
def test_pdfa_accepts_conformance_option(client, sample_pdf):
    """PDF/A endpoint accepts pdfa-1b conformance option."""
    response = client.post(
        "/pdfa",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": json.dumps({"conformance": "pdfa-1b"})},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


def test_pdfa_rejects_invalid_conformance(client, sample_pdf):
    """PDF/A endpoint returns 400 for invalid conformance level."""
    response = client.post(
        "/pdfa",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": json.dumps({"conformance": "invalid"})},
    )
    assert response.status_code == 400
    assert "Invalid conformance" in response.json()["error"]


@pytest.mark.skipif(not OCRMYPDF_AVAILABLE, reason="ocrmypdf CLI not installed")
def test_pdfa_preserves_filename(client, sample_pdf):
    """PDF/A endpoint includes the original filename in Content-Disposition."""
    response = client.post(
        "/pdfa",
        files={"file": ("my-doc.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": json.dumps({})},
    )
    assert response.status_code == 200
    assert "my-doc.pdf" in response.headers.get("content-disposition", "")


def test_pdfa_rejects_missing_file(client):
    """PDF/A endpoint returns 422 when no file is provided."""
    response = client.post("/pdfa")
    assert response.status_code == 422
