"""Regression coverage for response headers shared by every download route."""

from app.http_utils import attachment_headers, file_response


def test_attachment_header_supports_decomposed_portuguese_filename():
    # NFD, exactly as macOS uploads it: o + U+0303, c + U+0327.
    filename = "relato\u0303rio-conversac\u0327a\u0303o.pdf"

    response = file_response(b"%PDF", "application/pdf", filename, "output.pdf")

    disposition = response.headers["content-disposition"]
    assert 'filename="relatorio-conversacao.pdf"' in disposition
    # Composed on the way out: Windows and Linux expect NFC, and macOS
    # re-decomposes on save either way.
    assert "filename*=UTF-8''relat%C3%B5rio-conversa%C3%A7%C3%A3o.pdf" in disposition
    disposition.encode("ascii")


def test_attachment_header_strips_paths_quotes_and_controls():
    headers = attachment_headers('../../pasta\\relatorio"\r\n.pdf', "output.pdf")

    disposition = headers["Content-Disposition"]
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert "pasta" not in disposition
    assert 'filename="relatorio.pdf"' in disposition


def test_ascii_filename_comes_first():
    """RFC 6266 Appendix D: «filename should occur first, due to parsing
    problems in some existing implementations»."""
    disposition = attachment_headers("relatório.pdf", "output.pdf")["Content-Disposition"]

    assert disposition.index("filename=") < disposition.index("filename*=UTF-8''")


def test_name_that_folds_to_nothing_keeps_a_usable_ascii_fallback():
    """«文件.pdf» folds to a bare «.pdf» — an extension is not a filename."""
    disposition = attachment_headers("文件.pdf", "output.pdf")["Content-Disposition"]

    assert 'filename="output.pdf"' in disposition
    assert "filename*=UTF-8''%E6%96%87%E4%BB%B6.pdf" in disposition


def test_ext_value_delimiter_is_always_escaped():
    """A bare apostrophe would close the RFC 8187 charset''value framing."""
    disposition = attachment_headers("it's.pdf", "output.pdf")["Content-Disposition"]

    assert "filename*=UTF-8''it%27s.pdf" in disposition


def test_filename_length_is_bounded():
    """The name arrives from a caller, not from a disk. h11 caps a response's
    headers at 16 KiB, so an unbounded name is a response that never sends."""
    headers = attachment_headers(f"{'a' * 4000}.pdf", "output.pdf")

    disposition = headers["Content-Disposition"]
    assert len(disposition) < 600
    assert 'filename="' + "a" * 196 + '.pdf"' in disposition


def test_a_dot_far_from_the_end_is_not_an_extension():
    headers = attachment_headers(f"nota.{'a' * 4000}", "output.pdf")

    disposition = headers["Content-Disposition"]
    assert ".pdf" not in disposition
    assert len(disposition) < 600


def test_ascii_fallback_keeps_the_real_extension():
    """The media type is chosen from the same extension; the two must agree."""
    disposition = attachment_headers("文件.docx", "output.docx")["Content-Disposition"]

    assert 'filename="output.docx"' in disposition
    assert "filename*=UTF-8''%E6%96%87%E4%BB%B6.docx" in disposition


def test_percent_escape_is_neutralised_in_the_plain_filename():
    """RFC 6266 Appendix D: some parsers unescape "%XX" in `filename`."""
    disposition = attachment_headers("100%A9-final.pdf", "output.pdf")["Content-Disposition"]

    assert 'filename="100_A9-final.pdf"' in disposition
    assert "filename*=UTF-8''100%25A9-final.pdf" in disposition
