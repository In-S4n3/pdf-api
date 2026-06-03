"""POST /v2/redact/preview returns JSON match list (no PDF body)."""

import io
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
FIXTURE = Path(__file__).parent / "fixtures" / "sample_with_email.pdf"


def _post_preview(strategy="email", custom="", pattern=""):
    files = {"file": ("sample.pdf", io.BytesIO(FIXTURE.read_bytes()), "application/pdf")}
    return client.post(
        "/v2/redact/preview",
        files=files,
        data={
            "options": (
                f'{{"strategy":"{strategy}","customText":"{custom}","regexPattern":"{pattern}"}}'
            )
        },
        headers={"X-API-Key": "test-key"},
    )


def test_preview_returns_json_with_matches():
    r = _post_preview()
    assert r.status_code == 200, r.text
    body = r.json()
    assert "matches" in body and "total" in body
    assert body["total"] >= 2


def test_preview_matches_have_required_fields():
    r = _post_preview()
    for m in r.json()["matches"]:
        assert "id" in m and len(m["id"]) == 16
        assert "page" in m and isinstance(m["page"], int)
        assert "bbox" in m and len(m["bbox"]) == 4
        assert "kind" in m
        assert "context" in m
        assert "fullMatch" in m


def test_preview_no_matches_returns_empty_list():
    r = _post_preview(strategy="custom", custom="nothingmatches")
    assert r.status_code == 200
    body = r.json()
    assert body["matches"] == []
    assert body["total"] == 0


def test_preview_invalid_regex_400():
    r = _post_preview(strategy="regex", pattern="[unclosed")
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "invalid_regex_pattern"


def test_preview_encrypted_pdf_400():
    files = {
        "file": (
            "e.pdf",
            io.BytesIO((Path(__file__).parent / "fixtures" / "encrypted.pdf").read_bytes()),
            "application/pdf",
        )
    }
    r = client.post(
        "/v2/redact/preview",
        files=files,
        data={"options": '{"strategy":"email"}'},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "password_protected_pdf"
