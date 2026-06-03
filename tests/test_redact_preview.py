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


def test_apply_with_confirmed_ids_round_trips():
    """Preview returns IDs; apply with subset of those IDs redacts only the subset."""
    preview = _post_preview(strategy="email")
    matches = preview.json()["matches"]
    alice_ids = [m["id"] for m in matches if "alice" in m["fullMatch"]]
    assert alice_ids

    import json

    files = {"file": ("sample.pdf", io.BytesIO(FIXTURE.read_bytes()), "application/pdf")}
    r = client.post(
        "/v2/redact",
        files=files,
        data={"options": json.dumps({"strategy": "email", "confirmed_ids": alice_ids})},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200, r.text

    import pymupdf

    with pymupdf.open(stream=r.content, filetype="pdf") as doc:
        text = "\n".join(p.get_text("text") for p in doc)
    assert "alice" not in text.lower(), text
    assert "bob@example.org" in text  # bob NOT redacted (not in confirmed_ids)


def test_preview_missing_custom_text_returns_422_not_500():
    """Regression: model_validator's ValueError used to leak into FastAPI's
    response serializer and produce a 500. Verify it now returns a clean 422."""
    files = {"file": ("sample.pdf", io.BytesIO(FIXTURE.read_bytes()), "application/pdf")}
    r = client.post(
        "/v2/redact/preview",
        files=files,
        data={"options": '{"strategy":"custom","customText":""}'},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "invalid_options"
    # details must serialize cleanly — no ValueError object in payload
    assert isinstance(body["error"]["details"], list)


def test_preview_missing_regex_pattern_returns_422_not_500():
    files = {"file": ("sample.pdf", io.BytesIO(FIXTURE.read_bytes()), "application/pdf")}
    r = client.post(
        "/v2/redact/preview",
        files=files,
        data={"options": '{"strategy":"regex","regexPattern":""}'},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_options"


def test_apply_missing_custom_text_returns_422_not_500():
    """Same regression on the apply path."""
    files = {"file": ("sample.pdf", io.BytesIO(FIXTURE.read_bytes()), "application/pdf")}
    r = client.post(
        "/v2/redact",
        files=files,
        data={"options": '{"strategy":"custom","customText":""}'},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_options"
