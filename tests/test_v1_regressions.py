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


def test_v1_health_still_includes_request_id_header(client):
    """Cross-cutting observability should not break the existing payload."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-request-id"]
