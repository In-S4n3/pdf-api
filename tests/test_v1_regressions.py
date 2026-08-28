"""Regression tests for the legacy v1 contract."""

import io


def test_v1_invalid_options_returns_legacy_error_shape(client, sample_pdf):
    """Legacy routes should keep the flat {'error': message} contract."""
    response = client.post(
        "/ocr",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": "{"},
    )
    assert response.status_code == 400
    assert response.json() == {"error": "Options must be valid JSON."}


def test_v1_non_object_options_preserve_legacy_message(client, sample_pdf):
    response = client.post(
        "/ocr",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": "[]"},
    )
    assert response.status_code == 400
    assert response.json() == {"error": "Options payload must be a JSON object."}


def test_v1_upload_limit_preserves_legacy_message(client, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "16")
    response = client.post(
        "/compress",
        files={"file": ("big.pdf", io.BytesIO(b"%PDF-1.4\n" + b"A" * 32), "application/pdf")},
    )
    assert response.status_code == 413
    assert response.json() == {
        "error": "Uploaded file exceeds the configured limit of 16 bytes."
    }


def test_v1_health_still_includes_request_id_header(client):
    """Cross-cutting observability should not break the existing payload."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-request-id"]
