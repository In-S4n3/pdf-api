# tests/fixtures/gen_repair_fixtures.py
"""Generate the reparar-pdf test corpus from a clean multi-page base.

Run: python tests/fixtures/gen_repair_fixtures.py
Produces tests/fixtures/repair/*.pdf — deterministic, checked into git.
"""
import io
import zlib
from pathlib import Path

import pikepdf

OUT = Path(__file__).parent / "repair"
OUT.mkdir(exist_ok=True)


def _base(n_pages: int = 3) -> bytes:
    # blank N-page PDF via pikepdf (reportlab is not available)
    pdf = pikepdf.new()
    for _ in range(n_pages):
        pdf.add_blank_page(page_size=(612, 792))
    out = io.BytesIO()
    # static_id: qpdf's /ID trailer entry is otherwise randomized per save,
    # which would make this checked-in corpus non-reproducible on regen.
    pdf.save(out, static_id=True)
    pdf.close()
    return out.getvalue()


def _healthy() -> bytes:
    # normalized, clean 3-page PDF
    pdf = pikepdf.open(io.BytesIO(_base()))
    out = io.BytesIO()
    pdf.save(out, static_id=True)
    pdf.close()
    return out.getvalue()


def _broken_xref(clean: bytes) -> bytes:
    # corrupt the startxref offset so pikepdf/qpdf must reconstruct the xref table
    marker = clean.rfind(b"startxref")
    body = bytearray(clean)
    eol = body.find(b"\n", marker + len(b"startxref"))
    body[marker + len(b"startxref") : eol] = b"\n999999"
    return bytes(body)


def _truncated(clean: bytes) -> bytes:
    # drop the last 25% incl. the final page + %%EOF/xref
    return clean[: int(len(clean) * 0.75)]


def _stream_damaged(clean: bytes) -> bytes:
    # flip bytes inside a content stream so pikepdf opens but check flags it,
    # forcing Tier-2 escalation
    body = bytearray(clean)
    s = body.find(b"stream")
    if s != -1:
        body[s + 10 : s + 20] = b"\x00" * 10
    return bytes(body)


def _form() -> bytes:
    # a PDF WITH an AcroForm field (for the gs form-flattening warning path)
    pdf = pikepdf.open(io.BytesIO(_base(1)))
    pdf.Root.AcroForm = pdf.make_indirect(pikepdf.Dictionary(Fields=pikepdf.Array()))
    out = io.BytesIO()
    pdf.save(out, static_id=True)
    pdf.close()
    return out.getvalue()


def _bomb() -> bytes:
    # a small PDF whose content stream decompresses to a huge size (decompression bomb)
    huge = b"0 0 m\n" * (60 * 1024 * 1024 // 6)  # ~60MB decoded
    comp = zlib.compress(huge)
    return (
        b"%PDF-1.5\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>endobj\n"
        b"4 0 obj<</Length " + str(len(comp)).encode() + b"/Filter/FlateDecode>>stream\n"
        + comp + b"\nendstream endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF"
    )


def main() -> None:
    clean = _healthy()
    (OUT / "healthy.pdf").write_bytes(clean)
    (OUT / "broken_xref.pdf").write_bytes(_broken_xref(clean))
    (OUT / "truncated.pdf").write_bytes(_truncated(clean))
    (OUT / "stream_damaged.pdf").write_bytes(_stream_damaged(clean))
    (OUT / "form.pdf").write_bytes(_form())
    (OUT / "bomb.pdf").write_bytes(_bomb())
    print(f"wrote {len(list(OUT.glob('*.pdf')))} fixtures to {OUT}")


if __name__ == "__main__":
    main()
