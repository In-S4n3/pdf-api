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


def _pdf_with_an_xref_hole() -> bytes:
    """Objects 1, 2, 3 and 5: no xref section defines 4. Valid PDF (an undefined
    object is null) — what pyHanko-signed files and pdf-lib saves leave behind."""
    text = b"BT /F1 12 Tf 72 720 Td (Contact ana@example.com) Tj ET"
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R /Resources"
        b" << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>",
        5: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text),
    }
    pdf, offsets = bytearray(b"%PDF-1.7\n"), {}
    for number, body in objects.items():
        offsets[number] = len(pdf)
        pdf += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(pdf)
    pdf += b"xref\n0 4\n0000000000 65535 f \n"
    pdf += b"".join(b"%010d 00000 n \n" % offsets[n] for n in (1, 2, 3))
    pdf += b"5 1\n%010d 00000 n \n" % offsets[5]
    pdf += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(pdf)


def test_redact_a_pdf_whose_xref_skips_an_object_number():
    """scrub() read every object number and raised on 4: a 500 on pyHanko-signed files."""
    output = redact_pdf(_pdf_with_an_xref_hole(), strategy="email")
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        assert doc.page_count == 1
        assert "ana@example.com" not in doc[0].get_text()
        assert "Contact" in doc[0].get_text()


def _copies_left(pdf: bytes, secret: str) -> list[int]:
    """Objects whose source or decoded stream still holds secret, as text or hex."""
    needles = (secret.encode(), secret.encode().hex().encode(), secret.encode("utf-16-be"),
               secret.encode("utf-16-be").hex().encode())
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return [
            xref
            for xref in range(1, doc.xref_length())
            for data in [doc.xref_object(xref).encode("latin-1").lower()
                         + (doc.xref_stream(xref) or b"" if doc.xref_is_stream(xref) else b"")]
            if any(needle in data for needle in needles)
        ]


def test_redact_drops_the_signer_details_a_certification_signature_keeps():
    """/Perms and /DSS outlive the signature field: the signer's email stayed."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Signed by ana@example.com")
    sig, field = doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(sig, "<< /Type /Sig /ContactInfo (ana@example.com) >>")
    doc.update_object(field, f"<< /Type /Annot /Subtype /Widget /FT /Sig /T (Sig1) /V {sig} 0 R "
                             f"/F 132 /Rect [0 0 0 0] /P {page.xref} 0 R >>")  # how pyHanko signs
    doc.xref_set_key(page.xref, "Annots", f"[{field} 0 R]")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"<< /Fields [{field} 0 R] /SigFlags 3 >>")
    cert = doc.get_new_xref()
    doc.update_object(cert, "<<>>")
    doc.update_stream(cert, b"certificate of ana@example.com", new=True)
    doc.xref_set_key(doc.pdf_catalog(), "Perms", f"<< /DocMDP {sig} 0 R >>")
    doc.xref_set_key(doc.pdf_catalog(), "DSS", f"<< /Certs [{cert} 0 R] >>")
    source = doc.tobytes()
    assert len(_copies_left(source, "ana@example.com")) == 3

    assert _copies_left(redact_pdf(source, strategy="email"), "ana@example.com") == []

