"""scrub() integration — output must not leak via outline/metadata/etc."""

from pathlib import Path

import pymupdf

from app.services.pdf_tools import redact_pdf


def test_redact_scrubs_outline_bookmarks():
    """The outline must NOT contain secret text after redact, even when
    that text was only present in the bookmark (not in any page body)."""
    bytes_ = (Path(__file__).parent / "fixtures" / "with_bookmarks_pii.pdf").read_bytes()
    output = redact_pdf(bytes_, strategy="custom", custom_text="never-matches",
                        confirmed_ids=None)
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        toc = doc.get_toc()
        flat = " ".join(entry[1] for entry in toc)
        assert "secret-figure-12345" not in flat, f"outline still leaks: {flat!r}"


def test_redact_scrubs_metadata():
    bytes_ = (Path(__file__).parent / "fixtures" / "with_bookmarks_pii.pdf").read_bytes()
    output = redact_pdf(bytes_, strategy="custom", custom_text="never-matches",
                        confirmed_ids=None)
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        md = doc.metadata or {}
        assert md.get("title") in (None, "", "untitled"), md
        assert "secret-title-ABC" not in (md.get("title") or "")
        assert "secret-author-XYZ" not in (md.get("author") or "")
