"""Tests for POST /echo endpoint."""

import io


def test_echo_returns_uploaded_file(client, sample_pdf):
    """Echo endpoint returns the exact same bytes that were uploaded."""
    response = client.post(
        "/echo",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
    )
    assert response.status_code == 200
    assert response.content == sample_pdf
    assert response.headers["content-type"] == "application/pdf"


def test_echo_preserves_filename(client, sample_pdf):
    """Echo endpoint includes the original filename in Content-Disposition."""
    response = client.post(
        "/echo",
        files={"file": ("my-document.pdf", io.BytesIO(sample_pdf), "application/pdf")},
    )
    assert response.status_code == 200
    assert "my-document.pdf" in response.headers.get("content-disposition", "")


def test_echo_rejects_missing_file(client):
    """Echo endpoint returns 422 when no file is provided."""
    response = client.post("/echo")
    assert response.status_code == 422
