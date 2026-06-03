"""Match extraction — finds patterns and returns stable bboxes."""

from pathlib import Path

import pymupdf
import pytest

from app.services.pdf_tools import _extract_matches

FIXTURE = Path(__file__).parent / "fixtures" / "sample_with_email.pdf"


def _open():
    return pymupdf.open(stream=FIXTURE.read_bytes(), filetype="pdf")


def test_extract_email_finds_two_matches():
    with _open() as doc:
        matches = _extract_matches(doc, strategy="email", custom_text="", regex_pattern="")
    full_matches = {m.full_match for m in matches}
    assert full_matches == {"alice@example.com", "bob@example.org"}


def test_extract_phone_finds_one_match():
    with _open() as doc:
        matches = _extract_matches(doc, strategy="phone", custom_text="", regex_pattern="")
    # +351 912 345 678 — phone regex must catch this PT format
    assert any("912" in m.full_match for m in matches), [m.full_match for m in matches]


def test_extract_custom_text_case_insensitive():
    with _open() as doc:
        matches = _extract_matches(doc, strategy="custom", custom_text="ALICE", regex_pattern="")
    assert any(m.full_match.lower() == "alice" for m in matches), [m.full_match for m in matches]


def test_extract_regex_compiles_and_finds():
    with _open() as doc:
        matches = _extract_matches(doc, strategy="regex", custom_text="", regex_pattern=r"bob")
    assert any(m.full_match == "bob" for m in matches)


def test_extract_invalid_regex_raises_400():
    from app.api_errors import ApiError
    with _open() as doc:
        with pytest.raises(ApiError) as exc:
            _extract_matches(doc, strategy="regex", custom_text="", regex_pattern="[unclosed")
        assert exc.value.status_code == 400
        assert exc.value.code == "invalid_regex_pattern"


def test_extract_redos_pattern_times_out_400():
    """Catastrophic backtracking pattern must abort within timeout, not hang."""
    from app.api_errors import ApiError
    # Build a synthetic doc with content that triggers backtracking.
    # (a|a)+b on a long run of 'a' with no terminating 'b' forces the engine
    # to exhaust exponential alternatives before giving up.
    import pymupdf as _pm
    doc = _pm.open()
    page = doc.new_page()
    page.insert_text((72, 72), "a" * 100 + "X")  # no 'b' at end
    try:
        with pytest.raises(ApiError) as exc:
            _extract_matches(doc, strategy="regex", custom_text="", regex_pattern=r"(a|a)+b")
        assert exc.value.status_code == 400
        assert exc.value.code == "regex_too_slow"
    finally:
        doc.close()


def test_extract_match_ids_are_stable_across_calls():
    with _open() as doc:
        m1 = _extract_matches(doc, strategy="email", custom_text="", regex_pattern="")
    with _open() as doc:
        m2 = _extract_matches(doc, strategy="email", custom_text="", regex_pattern="")
    ids1 = sorted(m.id for m in m1)
    ids2 = sorted(m.id for m in m2)
    assert ids1 == ids2
