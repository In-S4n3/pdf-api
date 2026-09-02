"""Tests for POST /pdfa endpoint."""

import io
import json

import pytest

from tests._env import pdfa_resources_present

_NO_PDFA = "requires Ghostscript PDF/A resources (PDFA_def.ps + ICC; Docker-only)"


@pytest.mark.skipif(not pdfa_resources_present(), reason=_NO_PDFA)
def test_pdfa_returns_valid_pdf(client, sample_pdf):
    """PDF/A endpoint returns a valid PDF with default conformance."""
    response = client.post(
        "/pdfa",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": json.dumps({})},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


@pytest.mark.skipif(not pdfa_resources_present(), reason=_NO_PDFA)
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
    # API speaks Portuguese: "Nível de conformidade inválido: <x>. Suportados: ..."
    assert "inválido" in response.json()["error"]


@pytest.mark.skipif(not pdfa_resources_present(), reason=_NO_PDFA)
def test_pdfa_preserves_filename(client, sample_pdf):
    """PDF/A endpoint includes the original filename in Content-Disposition."""
    response = client.post(
        "/pdfa",
        files={"file": ("my-doc.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": json.dumps({})},
    )
    assert response.status_code == 200
    assert "my-doc.pdf" in response.headers.get("content-disposition", "")


@pytest.mark.skipif(not pdfa_resources_present(), reason=_NO_PDFA)
def test_pdfa_failure_message_is_portuguese(monkeypatch, sample_pdf):
    """TudoPDF renders a coded message verbatim, so it must not be English."""
    import subprocess

    from app.api_errors import ApiError
    from app.services import pdf_tools

    monkeypatch.setattr(
        pdf_tools,
        "_run_command",
        lambda command, **_: subprocess.CompletedProcess(command, 1, "", "boom"),
    )
    with pytest.raises(ApiError) as exc:
        pdf_tools.convert_pdf_to_pdfa(sample_pdf, "pdfa-2b")
    assert exc.value.code == "pdfa_conversion_failed"
    assert exc.value.message == "Falha na conversão para PDF/A."


def test_pdfa_rejects_missing_file(client):
    """PDF/A endpoint returns 422 when no file is provided."""
    response = client.post("/pdfa")
    assert response.status_code == 422
