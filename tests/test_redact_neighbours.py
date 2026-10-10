"""A redaction box removes the match and none of the glyphs beside it.

MuPDF removes every glyph whose box, 10% smaller on each side, touches the
redaction, and a glyph's box is as tall as the font's ascender and descender:
taller than tight leading, and a kerned full stop starts inside the letter
before it. Each case checks what stays, and that the match itself is gone.

What lies under the black box goes with it (decision D6, 2026-10-10): text
drawn over the match — a watermark, a stamp — may go. Text beside it — the
next word, a tightly led line — stays, or the redaction is refused.
"""

import io
import re

import pikepdf
import pymupdf
import pytest
from pikepdf import Array, Dictionary, Name

from app.api_errors import ApiError
from app.services.pdf_tools import _extract_matches, redact_pdf
from tests.test_redact_hidden_copies import _bytes, _drawn_text, _pdf


def _lines(output: bytes) -> list[str]:
    return [line.strip() for line in _drawn_text(output).splitlines() if line.strip()]


def _count(source: bytes, **options) -> int:
    with pymupdf.open(stream=source, filetype="pdf") as doc:
        return len(_extract_matches(doc, **{"custom_text": "", "regex_pattern": "", **options}))


# A line break, with or without a hyphen ending the line.
_BREAK = r"(?:[-­‐]?[ \t]*\n[ \t]*)?"


def _no_match_survives(output: bytes, needle: str) -> None:
    """The core promise: no reading of the result holds the match — with
    /ActualText or as drawn, off the page too, and across a line break."""
    pattern = re.compile(
        _BREAK.join(r"\s+" if c.isspace() else re.escape(c) for c in needle), re.IGNORECASE)
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        for page in doc:
            for flags in (0, pymupdf.TEXT_IGNORE_ACTUALTEXT):
                text = page.get_text(
                    flags=pymupdf.TEXTFLAGS_TEXT | flags, clip=pymupdf.INFINITE_RECT())
                assert not pattern.search(text), text


@pytest.mark.parametrize("leading", [14, 12, 11, 9])
def test_tight_leading_keeps_the_lines_above_and_below(leading):
    content = (b"BT /F1 12 Tf %d TL 72 700 Td (Contact Cont) Tj T* (Ana Silva today) Tj T*"
               b" (gypsy jig) Tj ET\n" % leading)
    source = _bytes(_pdf(content))
    # Words clipped to the hit took pieces of both lines: four matches for one.
    assert _count(source, strategy="custom", custom_text="Silva") == 1
    output = redact_pdf(source, strategy="custom", custom_text="Silva")
    assert _lines(output) == ["Contact Cont", "Ana", "today", "gypsy jig"]
    _no_match_survives(output, "Silva")


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


def _refused(source: bytes, **options) -> None:
    """Refused before payment, by the preview, and by the apply too: no box
    removes the match and leaves every glyph around it."""
    options = {"custom_text": "", "regex_pattern": "", **options}
    for attempt in (lambda: _count(source, **options), lambda: redact_pdf(source, **options)):
        with pytest.raises(ApiError) as refused:
            attempt()
        assert (refused.value.status_code, refused.value.code) == (422, "text_too_close")


def test_a_watermark_below_the_match_keeps_its_letters():
    """Its box reaches over the email's, its letters do not: a box that
    removes the email and spares it exists, and is the one used."""
    content = (b"BT /F1 12 Tf 72 700 Td (Contact ana@example.com today) Tj ET\n"
               b"BT /F1 60 Tf 110 652 Td (WM) Tj ET\n")
    output = redact_pdf(_bytes(_pdf(content)), strategy="email")
    assert _lines(output) == ["Contact", "today", "WM"]
    _no_match_survives(output, "ana@example.com")


@pytest.mark.parametrize("content", [
    # A watermark drawn over the line: its box covers the email's.
    b"BT /F1 12 Tf 72 700 Td (Contact ana@example.com today) Tj ET\n"
    b"BT /F1 60 Tf 110 690 Td (WM) Tj ET\n",
    # The same type drawn again half a point higher, over the email alone.
    b"BT /F1 12 Tf 72 700 Td (Contact ana@example.com today) Tj ET\n"
    b"BT /F1 12 Tf 150 700.5 Td (XXXX) Tj ET\n",
], ids=["watermark", "overprint"])
def test_text_drawn_over_the_match_goes_with_it(content):
    """No box removes the email and spares what is drawn over it: that goes
    too, as under any black box. The words beside it stay. Refused before."""
    output = redact_pdf(_bytes(_pdf(content)), strategy="email")
    assert _lines(output) == ["Contact", "today"]
    _no_match_survives(output, "ana@example.com")


@pytest.mark.parametrize("leading", [5, 4])
def test_lines_closer_than_half_the_type_are_refused(leading):
    """Lines above and below are beside the email, not over it. At 5 pt under
    12 pt type every band through the email touches one of them; at 4 pt the
    lines were taken for text drawn over the email, and lost letters."""
    content = (b"BT /F1 12 Tf %d TL 72 700 Td (Contact Cont) Tj T* (Ana ana@example.com) Tj T*"
               b" (gypsy jig) Tj ET\n" % leading)
    _refused(_bytes(_pdf(content)), strategy="email")


@pytest.mark.parametrize("angle", [90, 270])
@pytest.mark.parametrize("leading", [12, 5])
def test_turned_lines_keep_the_lines_beside_a_word(angle, leading):
    """Turned 90°, the lines beside «Silva» lie left and right of it on the
    page. Judged in the page's axes they were taken for text over it, and on
    5 pt leading «cial today» lost «da»: refused now, as upright."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        for i, line in enumerate(["Contact Cont", "Ana Silva", "cial today", "gypsy jig"]):
            page.insert_text((300, 400 + leading * i), line, fontsize=12,
                             morph=(pymupdf.Point(300, 400), pymupdf.Matrix(angle)))
        source = doc.tobytes()
    if leading == 5:
        _refused(source, strategy="custom", custom_text="Silva")
        return
    output = redact_pdf(source, strategy="custom", custom_text="Silva")
    assert _lines(output) == ["Contact Cont", "Ana", "cial today", "gypsy jig"]
    _no_match_survives(output, "Silva")


def test_a_glyph_beside_the_match_is_cut_off_on_the_side_that_keeps_it():
    """An «i» above «Word» and one below, 6 pt off, over its «W»: cut off above
    and below, no band was left, and «Word» was refused. Cut off at the side,
    both «i» stay."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), "Word", fontsize=12)
        page.insert_text((76, 94), "i", fontsize=12)
        page.insert_text((76, 106), "i", fontsize=12)
        source = doc.tobytes()
    output = redact_pdf(source, strategy="custom", custom_text="Word")
    assert _lines(output) == ["i", "i"]
    _no_match_survives(output, "Word")


def _short_font_pdf() -> bytes:
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
    return buf.getvalue()


def test_a_font_shorter_than_1_em_is_trimmed_by_the_box_mupdf_tests():
    """PyMuPDF stretches a glyph box to 1 em when the font's ascender and
    descender span less; MuPDF tests the short one. Trimmed off the line below
    by the stretched box, the box missed the email's glyphs. And the short
    boxes MuPDF tests lie inside those of «line» below: «ine» is under the
    email, and goes with it; «Below the l» stays, cut off at the side."""
    source = _short_font_pdf()
    with pymupdf.open(stream=source, filetype="pdf") as doc:
        span = doc[0].get_text("rawdict")["blocks"][0]["lines"][0]["spans"][0]
        assert span["ascender"] - span["descender"] < 1, "fixture must be a short font"
    output = redact_pdf(source, strategy="email")
    assert _lines(output) == ["Contact", "Below the l"]
    _no_match_survives(output, "ana@example.com")


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
        # The black box blanks no pixel (the whole box did): unstroked, as over no image.
        assert [d["type"] for d in doc[0].get_drawings()] == ["f"]
    scale = pix.width / 300
    dark = sum(pix.pixel(int(x * scale), int(y * scale))[0] < 128
               for x in range(int(ink.x0) + 1, int(ink.x1))
               for y in range(int(ink.y0) + 1, int(ink.y1)))
    assert dark == 0


def _grey(width: int, height: int) -> pymupdf.Pixmap:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pix.clear_with(230)
    return pix


_NEIGHBOURS = ["Contact Cont", "Ana Silva", "cial today", "gypsy jig"]
_WITHOUT = ["Contact Cont", "Ana", "cial today", "gypsy jig"]


def _over_an_image(lines: list[str], image: str, leading: float) -> bytes:
    """lines in 12 pt Helvetica over a grey letterhead filling the page, or
    beside a 10 pt logo drawn after them, its edge over the end of «Silva».
    A pixel per point at least: MuPDF blanks every pixel a box touches."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        if image == "letterhead":
            page.insert_image(page.rect, pixmap=_grey(612, 792))
        for i, line in enumerate(lines):
            page.insert_text((72, 100 + i * leading), line, fontsize=12)
        if image == "logo":
            end = 72 + pymupdf.get_text_length("Ana Silva", fontsize=12)
            page.insert_image(pymupdf.Rect(end - 2, 92 + leading, end + 8, 102 + leading),
                              pixmap=_grey(40, 40))
        return doc.tobytes()


@pytest.mark.parametrize("image", ["letterhead", "logo"])
@pytest.mark.parametrize("leading", [7, 9, 12, 14.4])
def test_visible_text_over_an_image_keeps_the_lines_beside_it(image, leading):
    """Over an image the box was kept whole, and took «tact C» and «oday» on
    normal 14.4 pt leading, even from a 10 pt logo touching the end of
    «Silva». And the page as a reader sees it: nothing darker than the page
    drawn with no «Silva» at all, outside the black box — no ink of the match
    left, and no stroke over the neighbours' (it blackened the letters above
    on 7 and 9 pt leading). The image is blanked, lighter, under the whole
    box."""
    source = _over_an_image(_NEIGHBOURS, image, leading)
    with pymupdf.open(stream=source, filetype="pdf") as doc:
        assert doc[0].get_image_info(), "fixture must draw an image"
    output = redact_pdf(source, strategy="custom", custom_text="Silva")
    assert _lines(output) == _WITHOUT
    _no_match_survives(output, "Silva")

    clip = pymupdf.Rect(60, 80, 220, 110 + 3 * leading)
    with (pymupdf.open(stream=output, filetype="pdf") as doc,
          pymupdf.open(stream=_over_an_image(_WITHOUT, image, leading)) as without):
        (box,) = [(d["rect"], d["fill"]) for d in doc[0].get_drawings()]
        shown, expected = (d[0].get_pixmap(dpi=288, clip=clip) for d in (doc, without))
    assert box[1] == (0, 0, 0)
    painted = (box[0] - (clip.x0, clip.y0, clip.x0, clip.y0)) * 4 + (-1, -1, 1, 1)  # antialiased
    n, width, ours, theirs = shown.n, shown.width, shown.samples, expected.samples
    darker = [pymupdf.Point(i // n % width, i // n // width) for i in range(0, len(ours), n)
              if ours[i] < theirs[i] - 16]
    assert darker and all(point in painted for point in darker)


def _scan_of(lines: list[str], leading: float) -> pymupdf.Pixmap:
    with pymupdf.open() as doc:
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 100 + i * leading), line, fontsize=12)
        return page.get_pixmap(dpi=144, colorspace=pymupdf.csGRAY)


def _text_over_a_scan(lines: list[str], layer: str, leading: float) -> bytes:
    """A scan of _NEIGHBOURS with lines laid over it the ways OCR tools do:
    hidden (3 Tr), white, or both on the same origins."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=_scan_of(_NEIGHBOURS, leading))
        for i, line in enumerate(lines):
            if layer in ("hidden", "twin"):
                page.insert_text((72, 100 + i * leading), line, fontsize=12, render_mode=3)
            if layer in ("white", "twin"):
                page.insert_text((72, 100 + i * leading), line, fontsize=12, color=(1, 1, 1))
        return doc.tobytes()


def _dark_pixels(pdf: bytes, clip: pymupdf.Rect) -> int:
    """Dark pixels of the page's first image, within clip (page points)."""
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        page = doc[0]
        pix = pymupdf.Pixmap(doc, page.get_images()[0][0])
        place = page.get_image_rects(page.get_images()[0][0])[0]
    sx, sy = pix.width / place.width, pix.height / place.height
    return sum(pix.pixel(int((x - place.x0) * sx), int((y - place.y0) * sy))[0] < 128
               for x in range(int(clip.x0) + 1, int(clip.x1))
               for y in range(int(clip.y0) + 1, int(clip.y1)))


@pytest.mark.parametrize("layer", ["hidden", "white", "twin"])
@pytest.mark.parametrize("leading", [7, 12, 14.4])
def test_text_over_a_scan_keeps_its_neighbours_and_blanks_the_match(layer, leading):
    """Over a scan the box was kept whole, so that it blanked the scan's
    pixels: the OCR text beside it lost «tact C» and «oday». Now the text goes
    under the box that spares its neighbours, the pixels under the whole box:
    no ink of «Silva» is left in the scan. A white or twinned OCR layer is
    text a reader does not see either."""
    source = _text_over_a_scan(_NEIGHBOURS, layer, leading)
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100 + leading), "Ana Silva", fontsize=12)
        ink = page.search_for("Silva")[0]
    assert _dark_pixels(source, ink) > 50, "fixture must show «Silva» in the scan"
    output = redact_pdf(source, strategy="custom", custom_text="Silva")
    assert _lines(output) == _lines(_text_over_a_scan(_WITHOUT, layer, leading))
    _no_match_survives(output, "Silva")
    assert _dark_pixels(output, ink) == 0


def _as_image_mask(text: pymupdf.Document, pix: pymupdf.Pixmap) -> bytes:
    """text's page with pix drawn over it as a 1-bit image mask, as a
    black-and-white scan often is: get_image_info lists it, get_bboxlog
    calls it fill-imgmask."""
    rows = []
    for y in range(pix.height):
        row = "".join("0" if v < 128 else "1" for v in pix.samples[y * pix.width:][:pix.width])
        row += "1" * (-len(row) % 8)
        rows.append(int(row, 2).to_bytes(len(row) // 8, "big"))
    with pikepdf.open(io.BytesIO(text.tobytes())) as pdf:
        page = pdf.pages[0]
        page.Resources.XObject = Dictionary(Scan=pdf.make_stream(
            b"".join(rows), Type=Name.XObject, Subtype=Name.Image, Width=pix.width,
            Height=pix.height, ImageMask=True, BitsPerComponent=1))
        page.contents_add(pdf.make_stream(b"0 g q 300 0 0 150 0 0 cm /Scan Do Q"))
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()


@pytest.mark.parametrize("cover", ["page", "half", "top", "mask"])
def test_text_an_image_is_drawn_over_keeps_the_whole_box_over_its_pixels(cover):
    """Visible glyphs an image is drawn over are not what a reader sees: the
    image is, and may show the match. Some scanners lay the scan over its
    text. Its pixels are blanked under the whole box, also where the image
    covers half the email, or a stripe over the top of its letters: trimmed
    there, the box left the tops of the email's letters in the scan."""
    lines = ["Contact Cont", "ana@example.com", "gypsy jig"]
    with pymupdf.open() as text:
        page = text.new_page(width=300, height=150)
        for i, line in enumerate(lines):
            page.insert_text((20, 40 + 16 * i), line, fontsize=14)
        ink = page.search_for("ana@example.com")[0]
        scan = pymupdf.Rect(0, 0, 300, 150)
        if cover == "half":
            scan.x0 = ink.x0 + ink.width / 2
        if cover == "top":
            scan.y1 = ink.y0 + ink.height / 2  # above the glyphs' middle
        pix = page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY, clip=scan)
        if cover == "mask":
            source = _as_image_mask(text, pix)
        else:
            page.insert_image(scan, pixmap=pix)  # drawn after the text, over it
            source = text.tobytes()

    def ink_left(pdf: bytes) -> int:
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            pix = pymupdf.Pixmap(doc, doc[0].get_images()[0][0])
        scale = pix.width / scan.width
        return sum((pix.pixel(int((x - scan.x0) * scale), int((y - scan.y0) * scale))[0] < 128)
                   != (cover == "mask")
                   for x in range(int(max(ink.x0, scan.x0)) + 1, int(ink.x1))
                   for y in range(int(ink.y0) + 1, int(min(ink.y1, scan.y1))))

    assert ink_left(source) > 50, "fixture must show the email in the image"
    output = redact_pdf(source, strategy="email")
    _no_match_survives(output, "ana@example.com")
    assert ink_left(output) == 0


def test_glyphs_under_actual_text_that_differs_still_go():
    """/ActualText longer than what the page draws: MuPDF gives each drawn
    glyph a character and puts the rest, of no width, after the last one. A
    full stop kerned over that last glyph must not save it: it lies on the
    «l», under the box, and goes with it. The word beside it stays."""
    content = (b"BT /F1 12 Tf 72 700 Td /Span <</ActualText (ana@example.com)>> BDC"
               b" (WWWWWWWWWWWWWl) Tj EMC [225 (.) -600 (today)] TJ ET\n")
    output = redact_pdf(_bytes(_pdf(content)), strategy="email")
    assert _lines(output) == ["today"]
    _no_match_survives(output, "ana@example.com")


def _no_width_pdf(content: bytes) -> bytes:
    """/Z draws every glyph 0 wide."""
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
    pdf.pages[0].obj.Contents = pdf.make_stream(content)
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def test_a_glyph_of_no_width_does_not_fail_the_redaction():
    """A read of the page without the edge clip kept a glyph of no width that
    MuPDF's own read dropped: pairing the two by position failed (HTTP 500)."""
    output = redact_pdf(_no_width_pdf(
        b"BT /Z 12 Tf 72 720 Td (x) Tj ET\n"
        b"BT /F1 12 Tf 72 700 Td (Contact ana@example.com) Tj ET\n"), strategy="email")
    assert "ana@example" not in _drawn_text(output)
    assert "Contact" in _drawn_text(output)


def test_a_match_with_a_glyph_of_no_width_is_refused():
    """The /Widths draw the email's «m» 0 wide; MuPDF tests it as wide as the
    font program's own «m» (a box over its 0-wide one kept it). Where it lies
    is not known here: no box is sure to take it and spare the lines beside.
    Left untrimmed, it took «Cont» from the line above."""
    _refused(_no_width_pdf(
        b"BT /F1 12 Tf 7 TL 72 707 Td (Contact Cont) Tj T* (ana@exa) Tj /Z 12 Tf (m) Tj"
        b" /F1 12 Tf (ple.com today) Tj T* (gypsy jig) Tj ET\n"), strategy="email")


@pytest.mark.parametrize(("first", "second", "options", "kept"), [
    # A word the line break hyphenated: the hyphen goes with it.
    (b"Este documento e confiden-", b"cial e nao pode sair",
     {"strategy": "custom", "custom_text": "confidencial"},
     ["Este documento e", "e nao pode sair"]),
    (b"Contact ana.silva@exam-", b"ple.com today", {"strategy": "email"}, ["Contact", "today"]),
    # A name whose own hyphen fell at the line break.
    (b"Assinado por Jean-", b"Pierre Dupont",
     {"strategy": "custom", "custom_text": "Jean-Pierre"}, ["Assinado por", "Dupont"]),
])
def test_a_match_hyphenated_across_a_line_break_is_redacted(first, second, options, kept):
    """MuPDF's search does not join «confiden-» and «cial»: the phrase was never
    found, and both halves stayed readable."""
    content = (b"BT /F1 12 Tf 12 TL 72 712 Td (Linha de cima) Tj T* (" + first + b") Tj T* ("
               + second + b") Tj T* (Linha de baixo) Tj ET\n")
    source = _bytes(_pdf(content))
    assert _count(source, **options) == 2  # one box per line
    output = redact_pdf(source, **options)
    assert _lines(output) == ["Linha de cima", *kept, "Linha de baixo"]
    _no_match_survives(output, options.get("custom_text") or "ana.silva@example.com")


def test_a_hyphen_between_digits_joins_nothing():
    """«2020-» then «2021» is a range, not one figure split by the line."""
    content = b"BT /F1 12 Tf 12 TL 72 712 Td (Periodo 912 345-) Tj T* (678 seguinte) Tj ET\n"
    source = _bytes(_pdf(content))
    assert _count(source, strategy="phone") == 0
    assert _lines(redact_pdf(source, strategy="phone")) == ["Periodo 912 345-", "678 seguinte"]


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_a_turned_page_loses_the_match_and_keeps_its_neighbours(rotation):
    """Every box the redaction reads and writes is unturned, as MuPDF removes."""
    pdf = _pdf(b"BT /F1 12 Tf 12 TL 72 700 Td (Contact Cont) Tj T* (Ana Silva today) Tj T*"
               b" (gypsy jig) Tj ET\n")
    pdf.pages[0].obj.Rotate = rotation
    output = redact_pdf(_bytes(pdf), strategy="custom", custom_text="Silva")
    assert _lines(output) == ["Contact Cont", "Ana", "today", "gypsy jig"]


def _codex(lines, leading=16) -> bytes:
    """Codex's fixture (PR 5b round 1): Helvetica 12 pt, one line per entry from (72, 100)."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 100 + i * leading), line, fontsize=12)
        return doc.tobytes()


@pytest.mark.parametrize(("first", "second"), [
    ((72, 100), (400, 100)), ((72, 100), (400, 500)),
    ((300, 100), (72, 114)),  # one block to MuPDF: the next line, in another column
    ((72, 100), (72, 120)),  # the cell below: two blocks to MuPDF
], ids=["next-cell", "other-block", "column-to-the-left", "cell-below"])
def test_a_hyphen_joins_only_the_line_that_carries_it_on(first, second):
    """«Jean-» in one table cell and «Pierre» in the next, or in a block far
    below: both were taken for one name, and both cells were blacked out."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text(first, "Jean-", fontsize=12)
        page.insert_text(second, "Pierre", fontsize=12)
        source = doc.tobytes()
    options = {"strategy": "custom", "custom_text": "Jean-Pierre"}
    assert _count(source, **options) == 0
    assert _lines(redact_pdf(source, **options)) == ["Jean-", "Pierre"]


def _turned(angle: int) -> bytes:
    with pymupdf.open() as doc:
        page = doc.new_page()
        for i, line in enumerate(["Linha de cima", "confiden-", "cial today"]):
            page.insert_text((300, 400 + 16 * i), line, fontsize=12,
                             morph=(pymupdf.Point(300, 400), pymupdf.Matrix(angle)))
        return doc.tobytes()


@pytest.mark.parametrize("angle", [90, 180, 270])
def test_a_turned_paragraph_joins_its_hyphenated_word(angle):
    """Text turned on the page: the next line was looked for below the one
    above, not where the turned text puts it — and MuPDF puts each turned
    line in a block of its own. Both halves stayed."""
    options = {"strategy": "custom", "custom_text": "confidencial"}
    assert _count(_turned(angle), **options) == 2  # one box per line
    output = redact_pdf(_turned(angle), **options)
    assert _lines(output) == ["Linha de cima", "today"]
    _no_match_survives(output, "confidencial")


@pytest.mark.parametrize(("jean", "pierre"), [
    (((212, 353), 0), ((411, 111), 45)),
    (((400, 300), 180), ((82.1, 483.6), 135)),
])
def test_a_line_never_carries_on_one_turned_otherwise(jean, pierre):
    """«Pierre» lands, in its own frame, just under «Jean-» in another one:
    different turns are no paragraph."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        for (origin, angle), word in ((jean, "Jean-"), (pierre, "Pierre")):
            page.insert_text(origin, word, fontsize=12,
                             morph=(pymupdf.Point(origin), pymupdf.Matrix(angle)))
        source = doc.tobytes()
    assert _count(source, strategy="custom", custom_text="Jean-Pierre") == 0


def test_a_diagonal_hyphenated_word_is_found_and_refused():
    """At 45° one upright box per line cannot take a word and spare the lines
    beside it — a word on one line is refused there too. Refused, not left
    readable unseen as before."""
    _refused(_turned(45), strategy="custom", custom_text="confidencial")


def test_upright_lines_mupdf_puts_in_two_blocks_never_join():
    """Upright lines of one block only: 20 pt apart under 12 pt type MuPDF reads
    two blocks — a cell below, or double spacing. ponytail: double-spaced
    text is not joined; a table's cells are told from it by nothing else."""
    source = _codex(["confiden-", "cial today"], 20)
    assert _count(source, strategy="custom", custom_text="confidencial") == 0


def test_a_line_of_mixed_sizes_still_joins():
    """A 20 pt «c» starts «confiden-»: the line's quad started 1.2 pt left of
    its box, and the line below no longer started at or left of it."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), "c", fontsize=20)
        page.insert_text((72 + pymupdf.get_text_length("c", fontsize=20), 100), "onfiden-",
                         fontsize=12)
        page.insert_text((72, 116), "cial today", fontsize=12)
        source = doc.tobytes()
    output = redact_pdf(source, strategy="custom", custom_text="confidencial")
    assert _lines(output) == ["today"]
    _no_match_survives(output, "confidencial")


def test_every_glyph_of_a_joined_match_goes_whatever_its_size():
    """«c» and «-» at 120 pt around «onfiden» at 12 pt: the band through the
    middle of the line passed over the small glyphs, and «onfiden» stayed."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        x = 72
        for piece, size in (("c", 120), ("onfiden", 12), ("-", 120)):
            page.insert_text((x, 200), piece, fontsize=size)
            x += pymupdf.get_text_length(piece, fontsize=size)
        page.insert_text((72, 216), "cial today", fontsize=12)  # one block
        source = doc.tobytes()
    output = redact_pdf(source, strategy="custom", custom_text="confidencial")
    assert _lines(output) == ["today"]
    _no_match_survives(output, "confidencial")


def test_each_hyphen_at_a_line_break_goes_or_stays_on_its_own():
    """«Jean-» keeps its hyphen and «confi-» loses it, in one phrase: neither
    every hyphen kept nor every one dropped found it."""
    source = _codex(["Jean-", "Pierre confi-", "dencial"])
    options = {"strategy": "custom", "custom_text": "Jean-Pierre confidencial"}
    assert _count(source, **options) == 4  # one box per word
    output = redact_pdf(source, **options)
    assert _lines(output) == []
    _no_match_survives(output, "Jean-Pierre confidencial")


@pytest.mark.parametrize("pattern", ["Silva|$", "Silva|(?=cial)"])
def test_an_empty_alternative_in_a_regex_matches_nothing(pattern):
    """«Silva|$» matches nothing at the page's end: that empty match pointed
    past the joined text, and the preview answered 500. Beside the joined
    line break, an empty match became an empty needle."""
    from app.router_v2 import _extract_matches_json

    source = _codex(["confiden-", "cial Silva"])
    preview = _extract_matches_json(
        source, strategy="regex", custom_text="", regex_pattern=pattern, match_cap=10)
    assert [m["context"] for m in preview["matches"]] == ["Silva"]
    output = redact_pdf(source, strategy="regex", regex_pattern=pattern)
    assert _lines(output) == ["confiden-", "cial"]


def test_tight_leading_keeps_every_neighbour_of_a_hyphenated_match():
    """7 pt leading under 12 pt type: the box took «Contact C», «today» and
    «gyp» with «confidencial»."""
    source = _codex(["Contact Cont", "confiden-", "cial today", "gypsy jig"], 7)
    assert _count(source, strategy="custom", custom_text="confidencial") == 2  # no piece twice
    output = redact_pdf(source, strategy="custom", custom_text="confidencial")
    assert _lines(output) == ["Contact Cont", "today", "gypsy jig"]
    _no_match_survives(output, "confidencial")


@pytest.mark.parametrize(("angle", "origin", "kept"), [
    # Its «N» crosses the email: it goes with it, and was refused once.
    (45, (60, 360), "CO FIDENCIAL"),
    # Every letter passes beside the email: the «F» went while a turned
    # glyph's box went untested — MuPDF tests it as for any other.
    (30, (140, 400), "CONFIDENCIAL"),
])
def test_a_diagonal_watermark_loses_only_the_letters_over_the_match(angle, origin, kept):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 300), "Contact ana@example.com today", fontsize=12)
        page.insert_text(origin, "CONFIDENCIAL", fontsize=48,
                         morph=(pymupdf.Point(origin), pymupdf.Matrix(angle)))
        source = doc.tobytes()
    output = redact_pdf(source, strategy="email")
    assert _lines(output) == ["Contact", "today", kept]
    _no_match_survives(output, "ana@example.com")


def test_the_lines_beside_come_before_a_letter_over_the_match():
    """A 60 pt «W» whose box, ascender to descender, reaches over «Silva» on
    7 pt leading. Spared first, it left no band between the lines above and
    below, and the redaction was refused. The lines are spared; the «W» goes."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        for i, line in enumerate(["Contact Cont", "Ana Silva", "cial today", "gypsy jig"]):
            page.insert_text((72, 100 + i * 7), line, fontsize=12)
        page.insert_text((82, 157), "W", fontsize=60)
        source = doc.tobytes()
    assert _lines(source)[-1] == "gypsy jigW"
    output = redact_pdf(source, strategy="custom", custom_text="Silva")
    assert _lines(output) == ["Contact Cont", "Ana", "cial today", "gypsy jig"]
    _no_match_survives(output, "Silva")


@pytest.mark.parametrize("leading", [7, 12])
def test_tight_leading_keeps_every_neighbour_of_a_word(leading):
    source = _codex(["Contact Cont", "Ana Silva", "cial today", "gypsy jig"], leading)
    output = redact_pdf(source, strategy="custom", custom_text="Silva")
    assert _lines(output) == ["Contact Cont", "Ana", "cial today", "gypsy jig"]
    _no_match_survives(output, "Silva")
