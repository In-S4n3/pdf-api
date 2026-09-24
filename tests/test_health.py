"""Tests for GET /health and /v2/health."""

import subprocess


def test_health_returns_200(client):
    """Health endpoint returns 200 with status ok."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def test_health_reports_status_only(client, monkeypatch):
    """No library versions (they told an unauthenticated caller which CVEs to
    try) and no subprocesses (each call used to start gs, tesseract and soffice)."""

    def no_subprocess(*args, **kwargs):
        raise AssertionError("health must not start a process")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    for path in ("/health", "/v2/health"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
