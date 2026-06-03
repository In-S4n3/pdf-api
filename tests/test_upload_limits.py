"""Upload size limit enforcement (regression test for streaming abort)."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_MAX_UPLOAD_BYTES, get_settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_default_max_upload_bytes_is_50_mib():
    settings = get_settings()
    assert settings.max_upload_bytes == DEFAULT_MAX_UPLOAD_BYTES == 50 * 1024 * 1024


def test_oversized_upload_returns_413(client, monkeypatch):
    """Streaming reader must abort with 413 once cumulative size exceeds limit."""
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "1024")  # 1 KiB cap for the test

    # 2 KiB payload — exceeds the 1 KiB env cap, must trigger 413
    oversized = b"\x25PDF-1.4\n" + b"A" * 2048
    files = {"file": ("big.pdf", io.BytesIO(oversized), "application/pdf")}

    response = client.post("/v2/compress", files=files, data={"options": "{}"})

    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "file_too_large"
    assert "1024" in body["error"]["message"]


def test_small_upload_passes_size_gate(client, monkeypatch, sample_pdf):
    """A payload below the limit must reach the tool handler (not 413)."""
    monkeypatch.setenv("MAX_UPLOAD_BYTES", str(len(sample_pdf) + 1024))
    files = {"file": ("sample.pdf", io.BytesIO(sample_pdf), "application/pdf")}

    response = client.post("/v2/compress", files=files, data={"options": "{}"})

    # Whatever compress decides (200 if OK, 4xx for tool-level reasons), it
    # must NOT be 413 — the streaming gate must let this small payload through.
    assert response.status_code != 413
