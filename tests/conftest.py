"""Shared test fixtures."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def development_auth_defaults(monkeypatch):
    """Tests opt into the local-dev auth posture unless a case overrides it."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("STRICT_API_KEY", raising=False)


@pytest.fixture
def sample_pdf() -> bytes:
    """Load the sample PDF fixture as bytes."""
    pdf_path = FIXTURES_DIR / "sample.pdf"
    return pdf_path.read_bytes()


@pytest.fixture
def sample_pdf_path() -> Path:
    """Path to sample PDF fixture."""
    return FIXTURES_DIR / "sample.pdf"
