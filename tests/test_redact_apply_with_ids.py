"""redact_pdf with confirmed_ids — applies only the specified subset."""

from pathlib import Path

import pymupdf
import pytest

from app.api_errors import ApiError
from app.services.pdf_tools import _extract_matches, redact_pdf

FIXTURE = Path(__file__).parent / "fixtures" / "sample_with_email.pdf"


def _matches_for(strategy="email") -> list:
    with pymupdf.open(stream=FIXTURE.read_bytes(), filetype="pdf") as doc:
        return _extract_matches(doc, strategy=strategy, custom_text="", regex_pattern="")


def _extract_text(pdf_bytes: bytes) -> str:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        return "\n".join(p.get_text("text") for p in doc)


def test_redact_without_confirmed_ids_redacts_all():
    output = redact_pdf(FIXTURE.read_bytes(), strategy="email", confirmed_ids=None)
    text = _extract_text(output)
    assert "alice@example.com" not in text
    assert "bob@example.org" not in text


def test_redact_with_subset_confirmed_ids_redacts_subset_only():
    matches = _matches_for("email")
    alice_ids = [m.id for m in matches if "alice" in m.full_match]
    assert alice_ids  # sanity

    output = redact_pdf(FIXTURE.read_bytes(), strategy="email", confirmed_ids=alice_ids)
    text = _extract_text(output)
    assert "alice" not in text.lower(), text
    assert "bob@example.org" in text


def test_redact_with_empty_confirmed_ids_redacts_nothing():
    output = redact_pdf(FIXTURE.read_bytes(), strategy="email", confirmed_ids=[])
    text = _extract_text(output)
    assert "alice@example.com" in text
    assert "bob@example.org" in text


def test_redact_with_unknown_ids_silently_skips():
    output = redact_pdf(FIXTURE.read_bytes(), strategy="email", confirmed_ids=["deadbeef00000000"])
    text = _extract_text(output)
    # Unknown ID → no redactions applied
    assert "alice@example.com" in text


def test_redact_encrypted_pdf_returns_400():
    bytes_ = (Path(__file__).parent / "fixtures" / "encrypted.pdf").read_bytes()
    with pytest.raises(ApiError) as exc:
        redact_pdf(bytes_, strategy="email", confirmed_ids=None)
    assert exc.value.code == "password_protected_pdf"
