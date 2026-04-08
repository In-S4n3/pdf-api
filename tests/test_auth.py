"""Tests for authentication helpers."""

import io


def test_echo_accepts_bearer_token_when_api_key_is_configured(client, monkeypatch, sample_pdf):
    """Authorization: Bearer should work alongside X-API-Key."""
    monkeypatch.setenv("API_KEY", "secret-token")
    response = client.post(
        "/echo",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    assert response.content == sample_pdf


def test_echo_rejects_missing_token_when_api_key_is_configured(client, monkeypatch, sample_pdf):
    """Configured API_KEY should require a matching credential."""
    monkeypatch.setenv("API_KEY", "secret-token")
    response = client.post(
        "/echo",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "X-API-Key header missing"


def test_echo_strict_mode_rejects_unconfigured_api_key(client, monkeypatch, sample_pdf):
    """STRICT_API_KEY should fail closed when the server is misconfigured."""
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("STRICT_API_KEY", "true")
    response = client.post(
        "/echo",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
    )
    assert response.status_code == 503
    assert response.json()["error"] == "API key is not configured"
