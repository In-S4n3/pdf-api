"""Match ID stability — same input must always yield same ID."""

from app.services.pdf_tools import _make_match


def test_match_id_is_deterministic():
    m1 = _make_match("email", 0, (10.0, 20.0, 100.0, 30.0), "x@y.com", "x@y.com")
    m2 = _make_match("email", 0, (10.0, 20.0, 100.0, 30.0), "x@y.com", "x@y.com")
    assert m1.id == m2.id


def test_match_id_changes_with_bbox():
    m1 = _make_match("email", 0, (10.0, 20.0, 100.0, 30.0), "x@y.com", "x@y.com")
    m2 = _make_match("email", 0, (11.0, 20.0, 100.0, 30.0), "x@y.com", "x@y.com")
    assert m1.id != m2.id


def test_match_id_changes_with_strategy():
    m1 = _make_match("email", 0, (10.0, 20.0, 100.0, 30.0), "x@y.com", "x@y.com")
    m2 = _make_match("custom", 0, (10.0, 20.0, 100.0, 30.0), "x@y.com", "x@y.com")
    assert m1.id != m2.id


def test_match_id_is_16_hex_chars():
    m = _make_match("email", 0, (10.0, 20.0, 100.0, 30.0), "x", "x")
    assert len(m.id) == 16
    int(m.id, 16)  # must parse as hex
