"""Tests for POST /redact endpoint."""

import io
import json

import pymupdf


def _make_pdf_with_text(text: str) -> bytes:
    """Create a PDF containing the given text for redaction testing."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    result = doc.tobytes()
    doc.close()
    return result


def _extract_text(content: bytes) -> str:
    """Extract all text from a PDF response body."""
    doc = pymupdf.open(stream=content, filetype="pdf")
    text = ""
    for page in doc:
        text += page.get_text("text")
    doc.close()
    return text


# -- Strategy: email --


def test_redact_email_strategy(client):
    """Redact with email strategy removes email addresses from PDF text."""
    pdf_bytes = _make_pdf_with_text("Contact us at user@example.com for info.")
    response = client.post(
        "/redact",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"strategy": "email"})},
    )
    assert response.status_code == 200
    text = _extract_text(response.content)
    assert "user@example.com" not in text


# -- Strategy: phone --


def test_redact_phone_strategy(client):
    """Redact with phone strategy removes phone numbers from PDF text."""
    pdf_bytes = _make_pdf_with_text("Call +351 912 345 678 for support.")
    response = client.post(
        "/redact",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"strategy": "phone"})},
    )
    assert response.status_code == 200
    text = _extract_text(response.content)
    assert "+351 912 345 678" not in text


# -- Strategy: custom --


def test_redact_custom_strategy_case_insensitive(client):
    """Redact with custom strategy removes text case-insensitively."""
    pdf_bytes = _make_pdf_with_text(
        "This is CONFIDENTIAL data. Also confidential here."
    )
    response = client.post(
        "/redact",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"strategy": "custom", "customText": "CONFIDENTIAL"})},
    )
    assert response.status_code == 200
    text = _extract_text(response.content)
    assert "CONFIDENTIAL" not in text
    assert "confidential" not in text


# -- Strategy: regex --


def test_redact_regex_strategy(client):
    """Redact with regex strategy removes text matching user-provided pattern."""
    pdf_bytes = _make_pdf_with_text("SSN: 123-45-6789 is sensitive.")
    response = client.post(
        "/redact",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"strategy": "regex", "regexPattern": r"\d{3}-\d{2}-\d{4}"})},
    )
    assert response.status_code == 200
    text = _extract_text(response.content)
    assert "123-45-6789" not in text


# -- Error cases --


def test_redact_invalid_regex_returns_400(client):
    """Redact with invalid regex pattern returns 400."""
    pdf_bytes = _make_pdf_with_text("Some text.")
    response = client.post(
        "/redact",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"strategy": "regex", "regexPattern": "[invalid"})},
    )
    assert response.status_code == 400


def test_redact_regex_too_long_returns_400(client):
    """Redact with regex pattern exceeding 500 chars returns 400."""
    pdf_bytes = _make_pdf_with_text("Some text.")
    long_pattern = "a" * 501
    response = client.post(
        "/redact",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"strategy": "regex", "regexPattern": long_pattern})},
    )
    assert response.status_code == 400


def test_redact_custom_missing_text_returns_400(client):
    """Redact with custom strategy but no customText returns 400."""
    pdf_bytes = _make_pdf_with_text("Some text.")
    response = client.post(
        "/redact",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"strategy": "custom"})},
    )
    assert response.status_code == 400


def test_redact_invalid_strategy_returns_400(client):
    """Redact with unknown strategy returns 400."""
    pdf_bytes = _make_pdf_with_text("Some text.")
    response = client.post(
        "/redact",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"strategy": "unknown"})},
    )
    assert response.status_code == 400


def test_redact_preserves_filename(client):
    """Redact endpoint includes the original filename in Content-Disposition."""
    pdf_bytes = _make_pdf_with_text("user@example.com")
    response = client.post(
        "/redact",
        files={"file": ("my-doc.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"strategy": "email"})},
    )
    assert response.status_code == 200
    assert "my-doc.pdf" in response.headers.get("content-disposition", "")


def test_redact_rejects_missing_file(client):
    """Redact endpoint returns 422 when no file is provided."""
    response = client.post("/redact")
    assert response.status_code == 422
