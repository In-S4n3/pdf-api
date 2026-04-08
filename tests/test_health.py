"""Tests for GET /health endpoint."""


def test_health_returns_200(client):
    """Health endpoint returns 200 with status ok."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def test_health_includes_versions(client):
    """Health endpoint includes version info for all libraries."""
    response = client.get("/health")
    data = response.json()
    versions = data["versions"]
    assert "pymupdf" in versions
    assert "pikepdf" in versions
    assert "ghostscript" in versions
    assert "tesseract" in versions
    assert "libreoffice" in versions


def test_health_handles_missing_binaries(client, monkeypatch):
    """Missing binaries should degrade to 'unavailable' instead of returning 500."""
    import subprocess

    def raise_missing_binary(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(subprocess, "run", raise_missing_binary)
    response = client.get("/health")
    assert response.status_code == 200
    versions = response.json()["versions"]
    assert versions["ghostscript"] == "unavailable"
    assert versions["tesseract"] == "unavailable"
    assert versions["libreoffice"] == "unavailable"
