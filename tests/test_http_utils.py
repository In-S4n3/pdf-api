"""Regression coverage for response headers shared by every download route."""

from app.http_utils import attachment_headers, file_response


def test_attachment_header_supports_decomposed_portuguese_filename():
    filename = "relato\u0303rio-conversac\u0327a\u0303o.pdf"

    response = file_response(b"%PDF", "application/pdf", filename, "output.pdf")

    disposition = response.headers["content-disposition"]
    assert 'filename="relatorio-conversacao.pdf"' in disposition
    assert "filename*=UTF-8''relato%CC%83rio-conversac%CC%A7a%CC%83o.pdf" in disposition
    disposition.encode("ascii")


def test_attachment_header_strips_paths_quotes_and_controls():
    headers = attachment_headers('../../pasta\\relatorio"\r\n.pdf', "output.pdf")

    disposition = headers["Content-Disposition"]
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert "pasta" not in disposition
    assert 'filename="relatorio.pdf"' in disposition
