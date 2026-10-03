"""A redaction box removes the match and none of the glyphs around it.

MuPDF removes every glyph whose box, 10% smaller on each side, touches the
redaction, and a glyph's box is as tall as the font's ascender and descender:
taller than tight leading, and a kerned full stop starts inside the letter
before it. Each case checks what stays, and that the match itself is gone.
"""

import io

import pikepdf
import pymupdf
import pytest
from pikepdf import Array, Dictionary, Name

from app.services.pdf_tools import _extract_matches, redact_pdf
from tests.test_redact_hidden_copies import _bytes, _drawn_text, _pdf


def _lines(output: bytes) -> list[str]:
    return [line.strip() for line in _drawn_text(output).splitlines() if line.strip()]


def _count(source: bytes, **options) -> int:
    with pymupdf.open(stream=source, filetype="pdf") as doc:
        return len(_extract_matches(doc, **{"custom_text": "", "regex_pattern": "", **options}))


@pytest.mark.parametrize("leading", [14, 12, 11, 9])
def test_tight_leading_keeps_the_lines_above_and_below(leading):
    content = (b"BT /F1 12 Tf %d TL 72 700 Td (Contact Cont) Tj T* (Ana Silva today) Tj T*"
               b" (gypsy jig) Tj ET\n" % leading)
    source = _bytes(_pdf(content))
    # Words clipped to the hit took pieces of both lines: four matches for one.
    assert _count(source, strategy="custom", custom_text="Silva") == 1
    output = redact_pdf(source, strategy="custom", custom_text="Silva")
    assert _lines(output) == ["Contact Cont", "Ana", "today", "gypsy jig"]


def test_the_black_box_stops_short_of_the_line_above():
    """Where the leading leaves room, the box stops at the line above's own
    box: at the edge MuPDF tests, the fill covered the tips of «gypsy». And
    unstroked: PyMuPDF's 1 pt stroke reached half a point past the box."""
    content = (b"BT /F1 12 Tf 12 TL 72 712 Td (gypsy jig) Tj T* (Ana Silva today) Tj T*"
               b" (Contact Cont) Tj ET\n")
    source = _bytes(_pdf(content))
    output = redact_pdf(source, strategy="custom", custom_text="Silva")
    above = pymupdf.Rect(0, 0, 612, 80 + 0.2 * 12)  # to the descenders of «gypsy jig»

    def pixels(pdf: bytes) -> bytes:
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            return doc[0].get_pixmap(clip=above, matrix=pymupdf.Matrix(8, 8)).samples

    assert pixels(output) == pixels(source)
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        assert [d["type"] for d in doc[0].get_drawings()] == ["f"]


@pytest.mark.parametrize("kerning", [80, 129, 180])
def test_a_kerned_full_stop_stays(kerning):
    content = b"BT /F1 12 Tf 72 700 Td [(Contact ana@example.TV) %d (.)] TJ ET\n" % kerning
    output = redact_pdf(_bytes(_pdf(content)), strategy="email")
    assert _lines(output) == ["Contact", "."]


@pytest.mark.parametrize("content", [
    # A watermark drawn over the line, from above and from below: its box
    # covers the email's.
    b"BT /F1 12 Tf 72 700 Td (Contact ana@example.com today) Tj ET\n"
    b"BT /F1 60 Tf 110 690 Td (WM) Tj ET\n",
    b"BT /F1 12 Tf 72 700 Td (Contact ana@example.com today) Tj ET\n"
    b"BT /F1 60 Tf 110 652 Td (WM) Tj ET\n",
    # Lines closer than half the font size.
    b"BT /F1 12 Tf 5 TL 72 700 Td (Contact Cont) Tj T* (Ana ana@example.com) Tj T*"
    b" (gypsy jig) Tj ET\n",
])
def test_text_over_the_match_never_keeps_it(content):
    output = redact_pdf(_bytes(_pdf(content)), strategy="email")
    assert "ana@example" not in _drawn_text(output)


def test_a_font_shorter_than_1_em_is_trimmed_by_the_box_mupdf_tests():
    """PyMuPDF stretches a glyph box to 1 em when the font's ascender and
    descender span less; MuPDF tests the short one. Trimmed off the line below
    by the stretched box, the box missed the email's glyphs."""
    pdf = pikepdf.new()
    short = Dictionary(
        Type=Name.FontDescriptor, FontName=Name.Short, Flags=32, ItalicAngle=0, StemV=80,
        FontBBox=Array([0, 0, 1000, 300]), Ascent=300, Descent=0, CapHeight=300)
    fonts = Dictionary(
        F1=pdf.make_indirect(Dictionary(
            Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica, FirstChar=32,
            LastChar=126, Widths=Array([556] * 95), FontDescriptor=pdf.make_indirect(short))),
        F2=pdf.make_indirect(Dictionary(
            Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)),
    )
    pdf.pages.append(pikepdf.Page(Dictionary(
        Type=Name.Page, MediaBox=Array([0, 0, 612, 792]), Resources=Dictionary(Font=fonts))))
    pdf.pages[0].obj.Contents = pdf.make_stream(
        b"BT /F1 12 Tf 72 700 Td (Contact ana@example.com) Tj ET\n"
        b"BT /F2 12 Tf 72 692 Td (Below the line) Tj ET\n")
    buf = io.BytesIO()
    pdf.save(buf)
    with pymupdf.open(stream=buf.getvalue(), filetype="pdf") as doc:
        span = doc[0].get_text("rawdict")["blocks"][0]["lines"][0]["spans"][0]
        assert span["ascender"] - span["descender"] < 1, "fixture must be a short font"
    assert "ana@example" not in _drawn_text(redact_pdf(buf.getvalue(), strategy="email"))


@pytest.mark.parametrize(("content", "kept"), [
    (b"BT /F1 12 Tf 72 710 Td (Above line) Tj ET\n", ["Above", "line", "x", "x", "Below", "line"]),
    # PyMuPDF drops a glyph whose moved box misses the page: the two reads of
    # the page differed in length, and the redaction failed.
    (b"BT /T3 12 Tf 72 787 Td (xxxx) Tj ET\n", ["xxxx", "x", "x", "Below", "line"]),
])
def test_a_type3_font_drawn_above_its_baseline_loses_the_match(content, kept):
    """PyMuPDF moved this font's boxes below where MuPDF places the glyphs:
    a box built from them kept every glyph of «Silva»."""
    pdf = pikepdf.new()
    glyph = pdf.make_stream(b"600 0 0 300 600 700 d1 0 300 600 400 re f")
    font = pdf.make_indirect(Dictionary(
        Type=Name.Font, Subtype=Name.Type3, FontBBox=Array([0, 300, 600, 700]),
        FontMatrix=Array([0.001, 0, 0, 0.001, 0, 0]), Resources=Dictionary(),
        CharProcs=Dictionary({f"/{c}": glyph for c in "Silvax"}),
        Encoding=Dictionary(Type=Name.Encoding, Differences=Array(
            [x for c in "Silvax" for x in (ord(c), Name(f"/{c}"))])),
        FirstChar=ord("S"), LastChar=ord("x"), Widths=Array([600] * (ord("x") - ord("S") + 1))))
    helvetica = pdf.make_indirect(Dictionary(
        Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    pdf.pages.append(pikepdf.Page(Dictionary(
        Type=Name.Page, MediaBox=Array([0, 0, 612, 792]),
        Resources=Dictionary(Font=Dictionary(T3=font, F1=helvetica)))))
    pdf.pages[0].obj.Contents = pdf.make_stream(
        content + b"BT /T3 12 Tf 72 700 Td (xSilvax) Tj ET\n"
        b"BT /F1 12 Tf 72 690 Td (Below line) Tj ET\n")
    buf = io.BytesIO()
    pdf.save(buf)
    output = redact_pdf(buf.getvalue(), strategy="custom", custom_text="Silva")
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        text = doc[0].get_text(clip=pymupdf.INFINITE_RECT(), flags=pymupdf.TEXTFLAGS_TEXT)
    assert text.split() == kept


@pytest.mark.parametrize("watermark", [False, True])
def test_an_ocr_layer_keeps_the_whole_box_over_the_scan(watermark):
    """A box over hidden OCR text blanks the scan's pixels too. Moved off the
    OCR line above, it left the tops of the email's letters in the image —
    also when visible text over the scan made the box look like plain text."""
    lines = ["Contact Cont", "ana@example.com", "gypsy jig"]
    text = pymupdf.open()
    page = text.new_page(width=300, height=150)
    for i, line in enumerate(lines):
        page.insert_text((20, 40 + 16 * i), line, fontsize=14)
    ink = page.search_for("ana@example.com")[0]
    pix = page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
    scan = pymupdf.open()
    page = scan.new_page(width=300, height=150)
    page.insert_image(page.rect, pixmap=pix)
    for i, line in enumerate(lines):
        page.insert_text((20, 40 + 16 * i), line, fontsize=14, render_mode=3)
    if watermark:
        page.insert_text((50, 65), "DRAFT", fontsize=30)

    output = redact_pdf(scan.tobytes(), strategy="email")
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        pix = pymupdf.Pixmap(doc, doc[0].get_images()[0][0])
        # Over an image the box keeps PyMuPDF's stroke, over the edge of what it blanks.
        assert "fs" in [d["type"] for d in doc[0].get_drawings()]
    scale = pix.width / 300
    dark = sum(pix.pixel(int(x * scale), int(y * scale))[0] < 128
               for x in range(int(ink.x0) + 1, int(ink.x1))
               for y in range(int(ink.y0) + 1, int(ink.y1)))
    assert dark == 0


def test_glyphs_under_actual_text_that_differs_still_go():
    """/ActualText longer than what the page draws: MuPDF gives each drawn
    glyph a character and puts the rest, of no width, after the last one. A
    full stop kerned over that last glyph must not save it."""
    content = (b"BT /F1 12 Tf 72 700 Td /Span <</ActualText (ana@example.com)>> BDC"
               b" (WWWWWWWWWWWWWl) Tj EMC [225 (.)] TJ ET\n")
    output = redact_pdf(_bytes(_pdf(content)), strategy="email")
    assert "l" not in _drawn_text(output)


def test_a_glyph_of_no_width_does_not_fail_the_redaction():
    """A read of the page without the edge clip kept a glyph of no width that
    MuPDF's own read dropped: pairing the two by position failed (HTTP 500)."""
    pdf = pikepdf.new()
    flat = Dictionary(
        Type=Name.FontDescriptor, FontName=Name.Flat, Flags=32, ItalicAngle=0, StemV=80,
        FontBBox=Array([0, 0, 1000, 300]), Ascent=300, Descent=0, CapHeight=300)
    fonts = Dictionary(
        Z=pdf.make_indirect(Dictionary(
            Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica, FirstChar=32,
            LastChar=126, Widths=Array([0] * 95), FontDescriptor=pdf.make_indirect(flat))),
        F1=pdf.make_indirect(Dictionary(
            Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)),
    )
    pdf.pages.append(pikepdf.Page(Dictionary(
        Type=Name.Page, MediaBox=Array([0, 0, 612, 792]), Resources=Dictionary(Font=fonts))))
    pdf.pages[0].obj.Contents = pdf.make_stream(
        b"BT /Z 12 Tf 72 720 Td (x) Tj ET\n"
        b"BT /F1 12 Tf 72 700 Td (Contact ana@example.com) Tj ET\n")
    buf = io.BytesIO()
    pdf.save(buf)
    output = redact_pdf(buf.getvalue(), strategy="email")
    assert "ana@example" not in _drawn_text(output)
    assert "Contact" in _drawn_text(output)
