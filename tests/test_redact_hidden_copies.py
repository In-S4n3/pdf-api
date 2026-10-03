"""Copies of the redacted text that no page draws must go with it.

Each fixture hides ana@example.com in one place a reader or an extractor can
still reach after the drawn text is gone, then checks every object of the
output. Unrelated text in the same places must stay.
"""

import io
import tracemalloc
import zlib

import pikepdf
import pymupdf
import pytest
from pikepdf import Array, Dictionary, Name, String

from app.api_errors import ApiError
from app.services.pdf_tools import _extract_matches, redact_pdf
from tests.test_redact_scrub import _copies_left

SECRET = "ana@example.com"
SHOWN = b"BT /F1 12 Tf 72 700 Td (Contact ana@example.com today) Tj ET\n"
OTHER = b"BT /F1 12 Tf 72 650 Td (Unrelated line) Tj ET\n"


def _pdf(content: bytes = SHOWN + OTHER, pages: int = 1):
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    for _ in range(pages):
        pdf.pages.append(pikepdf.Page(Dictionary(
            Type=Name.Page, MediaBox=Array([0, 0, 612, 792]),
            Resources=Dictionary(Font=Dictionary(F1=font)),
        )))
        pdf.pages[-1].obj.Contents = pdf.make_stream(content)
    return pdf


def _bytes(pdf) -> bytes:
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _tagged(alternatives: dict):
    """A tagged page: one StructElem per line, `alternatives` on the first one."""
    pdf = _pdf(b"/P <</MCID 0>> BDC\n" + SHOWN + b"EMC\n/P <</MCID 1>> BDC\n" + OTHER + b"EMC\n")
    page = pdf.pages[0].obj
    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    shown = pdf.make_indirect(Dictionary(S=Name.P, P=root, Pg=page, K=0, **alternatives))
    other = pdf.make_indirect(
        Dictionary(S=Name.Figure, P=root, Pg=page, K=1, Alt=String("Company logo")))
    root.K = Array([shown, other])
    root.ParentTree = pdf.make_indirect(Dictionary(Nums=Array([0, Array([shown, other])])))
    page.StructParents = 0
    pdf.Root.StructTreeRoot = root
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    return pdf


def _redacted(pdf) -> bytes:
    source = _bytes(pdf)
    assert _copies_left(source, SECRET), "fixture must hold the secret"
    return redact_pdf(source, strategy="email")


def _drawn_text(output: bytes) -> str:
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        return "".join(page.get_text(flags=pymupdf.TEXT_IGNORE_ACTUALTEXT) for page in doc)


def _struct_elems(output: bytes) -> list:
    with pikepdf.open(io.BytesIO(output)) as pdf:
        return [dict(o.items()) for o in pdf.objects
                if isinstance(o, pikepdf.Dictionary) and "/S" in o and "/Pg" in o]


@pytest.mark.parametrize("alternatives", [
    {"ActualText": String("Contact ana@example.com today")},
    {"ActualText": String("Contact ana@example.com today 中")},  # stored UTF-16
    {"E": String("ana@example.com")},
    {"T": String("ana@example.com")},
    {"ID": String("ana@example.com")},
], ids=["ActualText", "ActualText-utf16", "E", "T", "ID"])
def test_struct_element_keeps_no_copy(alternatives):
    """MuPDF wrote the edited /ActualText into /Alt and left the original: text
    extraction of the 'redacted' file still returned the email."""
    output = _redacted(_tagged(alternatives))
    assert _copies_left(output, SECRET) == []
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        assert SECRET not in doc[0].get_text()


def test_replacement_text_goes_and_image_descriptions_stay():
    """Owner's call (hybrid): /ActualText is a copy of the page text, so it goes
    everywhere; a figure's /Alt describes, so it stays unless it holds a match."""
    output = _redacted(_tagged({"ActualText": String("Contact ana@example.com today")}))
    elems = _struct_elems(output)
    assert not any("/ActualText" in e for e in elems)
    assert [str(e["/Alt"]) for e in elems if "/Alt" in e] == ["Company logo"]


def test_hidden_only_description_loses_just_the_match():
    """No page draws the email: only the figure's description holds it."""
    pdf = _tagged({})
    figure = pdf.Root.StructTreeRoot.K[1]
    figure.Alt = String("Photo sent by ana@example.com in May")
    output = _redacted(pdf)
    assert _copies_left(output, SECRET) == []
    alts = [str(e["/Alt"]) for e in _struct_elems(output) if "/Alt" in e]
    assert alts == ["Photo sent by  in May"]


def test_an_attribute_value_held_in_its_own_object_loses_the_match():
    """A table header's /Headers names element ids, and the array can be indirect."""
    pdf = _tagged({})
    headers = pdf.make_indirect(Array([String(SECRET), String("col-2")]))
    pdf.Root.StructTreeRoot.K[0].A = Dictionary(O=Name.Table, Headers=headers)
    output = _redacted(pdf)
    assert _copies_left(output, SECRET) == []
    assert any(e.get("/A") for e in _struct_elems(output)), "the attribute stays"


def _table(pdf):
    """The tagged page's two elements as a table's header and data cells."""
    root = pdf.Root.StructTreeRoot
    header, cell = root.K
    header.S, cell.S = Name.TH, Name.TD
    table = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Table, P=root))
    row = pdf.make_indirect(
        Dictionary(Type=Name.StructElem, S=Name.TR, P=table, K=Array([header, cell])))
    table.K, header.P, cell.P, root.K = row, row, row, table
    return table, header


def test_class_attributes_and_id_tree_limits_lose_the_match():
    pdf = _tagged({})
    table, header = _table(pdf)
    root = pdf.Root.StructTreeRoot
    root.ClassMap = Dictionary(T1=Dictionary(O=Name.Table, Summary=String(f"Owner {SECRET}")))
    table.C = Name.T1
    header.ID = String(SECRET)
    leaf = pdf.make_indirect(Dictionary(
        Names=Array([String(SECRET), header]), Limits=Array([String(SECRET), String(SECRET)])))
    root.IDTree = Dictionary(Kids=Array([leaf]))
    output = _redacted(pdf)
    assert _copies_left(output, SECRET) == []
    with pikepdf.open(io.BytesIO(output)) as out:
        assert str(out.Root.StructTreeRoot.ClassMap.T1.Summary) == "Owner "


def test_a_pattern_too_slow_for_a_hidden_string_empties_it_after_payment():
    """The preview never ran the pattern on /Alt; refusing now would be after payment."""
    pdf = _tagged({})
    pdf.Root.StructTreeRoot.K[1].Alt = String("a" * 15000 + "!")
    output = redact_pdf(_bytes(pdf), strategy="regex", regex_pattern=r"ana@example\.com|(a+)+$")
    assert _copies_left(output, SECRET) == []
    assert [str(e["/Alt"]) for e in _struct_elems(output) if "/Alt" in e] == [""]


def test_visible_email_under_a_different_replacement_text_is_redacted():
    """MuPDF extracts /ActualText instead of the glyphs: «ana at example dot com»
    hid the drawn ana@example.com from the matcher, and the page still showed it."""
    output = _redacted(_tagged({"ActualText": String("Contact ana at example dot com today")}))
    assert SECRET not in _drawn_text(output)
    assert "Unrelated line" in _drawn_text(output)


@pytest.mark.parametrize("key", [b"ActualText", b"Alt", b"E"])
def test_inline_marked_content_keeps_no_copy(key):
    pdf = _pdf(b"/Span <</" + key + b" (Contact ana@example.com today)>> BDC\n" + SHOWN
               + b"EMC\n/Span <</Lang (pt-PT) /Alt (Unrelated)>> BDC\n" + OTHER + b"EMC\n")
    output = _redacted(pdf)
    assert _copies_left(output, SECRET) == []
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        content = doc[0].read_contents()
    assert b"/Lang(pt-PT)" in content.replace(b" ", b"") and b"(Unrelated)" in content


def test_named_property_list_and_designated_point_keep_no_copy():
    pdf = _pdf(b"/Span /P1 BDC\n" + SHOWN + b"EMC\n/Note <</Text (ana@example.com)>> DP\n" + OTHER)
    pdf.pages[0].obj.Resources.Properties = Dictionary(
        P1=Dictionary(ActualText=String("Contact ana@example.com today")))
    assert _copies_left(_redacted(pdf), SECRET) == []


def _attach(pdf, holder, data: bytes):
    file = pdf.make_stream(data)
    file.Type = Name.EmbeddedFile
    spec = Dictionary(Type=Name.Filespec, F=String("data.xml"), EF=Dictionary(F=file),
                      AFRelationship=Name.Data)
    holder.AF = Array([pdf.make_indirect(spec)])


def test_payloads_no_page_draws_are_dropped():
    """Associated files (a Factur-X invoice XML), application private data
    (Illustrator keeps the whole source file there), actions and named
    destinations all held the email after redaction."""
    pdf = _pdf()
    page = pdf.pages[0].obj
    _attach(pdf, pdf.Root, b"<Invoice><Email>ana@example.com</Email></Invoice>")
    _attach(pdf, page, b"customer: ana@example.com")
    page.PieceInfo = Dictionary(Illustrator=Dictionary(
        LastModified=String("D:2026"), Private=pdf.make_stream(b"artwork by ana@example.com")))
    pdf.Root.OpenAction = Dictionary(S=Name.URI, URI=String("mailto:ana@example.com"))
    page.AA = Dictionary(O=Dictionary(S=Name.URI, URI=String("mailto:ana@example.com")))
    dests = Array([String(SECRET), Array([page, Name.Fit])])
    pdf.Root.Names = Dictionary(Dests=Dictionary(Names=dests))
    output = _redacted(pdf)
    assert _copies_left(output, SECRET) == []
    assert "Unrelated line" in _drawn_text(output)


def test_a_colour_space_named_af_survives():
    """The withdrawn attempt deleted keys by name anywhere: a font named /E vanished."""
    pdf = _pdf(b"/AF cs 0 sc 72 72 50 50 re f\n" + SHOWN)
    pdf.pages[0].obj.Resources.ColorSpace = Dictionary(AF=Array([Name.Indexed, Name.DeviceRGB, 0,
                                                                 String(b"\x00\x00\xff")]))
    output = _redacted(pdf)
    with pikepdf.open(io.BytesIO(output)) as out:
        assert "/AF" in out.pages[0].Resources.ColorSpace


def test_layer_names_and_page_labels_lose_just_the_match():
    pdf = _pdf()
    layer = pdf.make_indirect(Dictionary(Type=Name.OCG, Name=String("notes for ana@example.com")))
    pdf.Root.OCProperties = Dictionary(OCGs=Array([layer]), D=Dictionary(ON=Array([layer])))
    label = Dictionary(P=String("ana@example.com-"), S=Name.D)
    pdf.Root.PageLabels = Dictionary(Nums=Array([0, label]))
    output = _redacted(pdf)
    assert _copies_left(output, SECRET) == []
    with pikepdf.open(io.BytesIO(output)) as out:
        assert str(out.Root.OCProperties.OCGs[0].Name) == "notes for "
        assert str(out.Root.PageLabels.Nums[1].P) == "-"


def _hidden_layer_pdf(how: str = "off"):
    hidden = b"/OC /oc1 BDC BT /F1 12 Tf 72 600 Td (Hidden ana@example.com) Tj ET EMC\n"
    pdf = _pdf(SHOWN + hidden + OTHER)
    layer = pdf.make_indirect(Dictionary(Type=Name.OCG, Name=String("Layer")))
    config, marked = Dictionary(OFF=Array([layer])), layer
    if how == "view-usage":  # print-only: /AS applies the layer's own /ViewState
        del config.OFF
        layer.Usage = Dictionary(View=Dictionary(ViewState=Name.OFF))
        config.AS = Array([Dictionary(
            Event=Name.View, Category=Array([Name.View]), OCGs=Array([layer]))])
    elif how == "shown-while-off":  # visible, and gone the moment the layer is on
        marked = pdf.make_indirect(
            Dictionary(Type=Name.OCMD, OCGs=Array([layer]), P=Name.AllOff))
    pdf.Root.OCProperties = Dictionary(OCGs=Array([layer]), D=config)
    pdf.pages[0].obj.Resources.Properties = Dictionary(oc1=marked)
    return pdf


@pytest.mark.parametrize("how", ["off", "view-usage", "shown-while-off"])
def test_text_in_a_layer_that_is_off_is_found_and_redacted(how):
    """Any reader can switch the layer on; MuPDF's extraction skipped it."""
    source = _bytes(_hidden_layer_pdf(how))
    with pymupdf.open(stream=source, filetype="pdf") as doc:
        assert len(_extract_matches(doc, strategy="email", custom_text="", regex_pattern="")) == 2
        shown = "Hidden" in doc[0].get_text()
    output = redact_pdf(source, strategy="email")
    assert _copies_left(output, SECRET) == []
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        assert ("Hidden" in doc[0].get_text()) == shown, "the layers show as before"


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_text_outside_the_visible_page_is_removed(rotation):
    """Off the page and below the CropBox: no viewer shows it, extraction gets it."""
    pdf = _pdf(SHOWN + b"BT /F1 12 Tf -400 400 Td (Offpage ana@example.com) Tj ET\n"
               b"BT /F1 12 Tf 72 20 Td (Below ana@example.com) Tj ET\n"
               b"BT /F1 12 Tf 400 120 Td (Bottom right corner) Tj ET\n" + OTHER)
    pdf.pages[0].obj.CropBox = Array([0, 50, 612, 792])
    pdf.pages[0].obj.Rotate = rotation
    output = _redacted(pdf)
    assert _copies_left(output, SECRET) == []
    assert "Bottom right corner" in _drawn_text(output)
    assert "Unrelated line" in _drawn_text(output)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("box", ["CropBox", "MediaBox"])
def test_a_page_box_away_from_the_origin_keeps_its_visible_text(box, rotation):
    """clip_to_rect mapped the box the wrong way and blanked the whole page."""
    pdf = _pdf(b"BT /F1 12 Tf 400 500 Td (Visible words) Tj ET\n"
               b"BT /F1 12 Tf -400 500 Td (Offpage ana@example.com) Tj ET\n")
    setattr(pdf.pages[0].obj, box, Array([100, 50, 500, 750]))
    pdf.pages[0].obj.Rotate = rotation
    output = _redacted(pdf)
    assert _copies_left(output, SECRET) == []
    assert "Visible words" in _drawn_text(output)


def _pattern_pdf(cell_text: bytes, fill: bytes = b"72 500 300 50", at: bytes = b"0 5"):
    paint = b"/Pattern cs /Pt1 scn " + fill + b" re f 0 g\n" if fill else b""
    pdf = _pdf(SHOWN + paint + OTHER)
    resources = pdf.pages[0].obj.Resources
    cell = pdf.make_stream(b"BT /F1 10 Tf " + at + b" Td (" + cell_text + b") Tj ET")
    cell.Type, cell.PatternType, cell.PaintType, cell.TilingType = Name.Pattern, 1, 1, 1
    cell.BBox, cell.XStep, cell.YStep = Array([0, 0, 200, 40]), 200, 40
    cell.Resources = Dictionary(Font=Dictionary(F1=resources.Font.F1))
    resources.Pattern = Dictionary(Pt1=cell)
    return _bytes(pdf)


@pytest.mark.parametrize("fill, at", [
    (b"72 500 300 50", b"0 5"),
    (b"0 0 612 792", b"20 20"),  # its text extracts like the page's own
], ids=["cell-edge", "whole-page"])
def test_a_match_inside_a_fill_pattern_is_refused_before_payment(fill, at):
    """MuPDF neither places nor removes text in a tiling pattern cell; a box at the
    coordinates it reports blacked out unrelated text and left the email."""
    source = _pattern_pdf(b"ana@example.com", fill, at)
    with pymupdf.open(stream=source, filetype="pdf") as doc, pytest.raises(ApiError) as preview:
        _extract_matches(doc, strategy="email", custom_text="", regex_pattern="")
    assert preview.value.status_code == 422
    with pytest.raises(ApiError) as apply:
        redact_pdf(source, strategy="email")
    assert apply.value.code == preview.value.code == "text_in_fill_pattern"


def test_a_match_in_a_fill_pattern_that_paints_nothing_is_refused():
    """A zero-area fill draws and extracts nothing, and the cell kept the email."""
    with pytest.raises(ApiError) as refused:
        redact_pdf(_pattern_pdf(b"ana@example.com", fill=b"0 0 0 0"), strategy="email")
    assert refused.value.code == "text_in_fill_pattern"


def test_unrelated_text_inside_a_fill_pattern_is_fine():
    output = redact_pdf(_pattern_pdf(b"CONFIDENTIAL"), strategy="email")
    assert _copies_left(output, SECRET) == []
    assert "Unrelated line" in _drawn_text(output)


def _picture(text: str) -> bytes:
    with pymupdf.open() as src:
        page = src.new_page(width=400, height=100)
        page.insert_text((10, 60), text, fontsize=30)
        return page.get_pixmap(dpi=144).tobytes("png")


def _dark_pixels(doc, xref: int) -> int:
    pix = pymupdf.Pixmap(doc, xref)
    return sum(1 for i in range(0, len(pix.samples), pix.n) if pix.samples[i] < 100)


def _host(picture: bytes):
    """A one-page document drawing picture, to show through a form XObject."""
    host = pymupdf.open()
    host.new_page(width=400, height=100).insert_image(pymupdf.Rect(0, 0, 400, 100), stream=picture)
    return host


@pytest.mark.parametrize("through_form", [False, True], ids=["direct", "form-xobject"])
def test_an_image_shown_cropped_on_another_page_loses_the_redacted_pixels(through_form):
    """Redaction blanks a copy of the image for its page; another page drawing
    the same image kept the original, the email intact outside its crop."""
    photo = _picture(SECRET)
    with pymupdf.open() as doc, _host(photo) as host:
        for _ in range(2):
            doc.new_page(width=400, height=100)
        first, second = doc[0], doc[1]
        if through_form:  # PyMuPDF reuses one form XObject for both
            first.show_pdf_page(first.rect, host, 0)
            second.show_pdf_page(second.rect, host, 0)
        else:
            second.insert_image(second.rect, xref=first.insert_image(first.rect, stream=photo))
        first.insert_text((10, 60), SECRET, fontsize=30, render_mode=3)  # its OCR layer
        doc.xref_set_key(second.xref, "CropBox", "[0 60 40 100]")  # a corner, no text
        source = doc.tobytes()
    output = redact_pdf(source, strategy="email")
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        images = {item[0] for page in doc for item in page.get_images(full=True)}
        assert images
        assert {image: _dark_pixels(doc, image) for image in images} == dict.fromkeys(images, 0)


def test_a_shared_logo_the_redaction_misses_stays_intact():
    """A copy pairs with the image it replaced by size only when that image left
    the page: a same-size logo still drawn there must not take the photo."""
    logo, photo = _picture("LOGO"), _picture(SECRET)
    with pymupdf.open() as doc:
        first = doc.new_page(width=400, height=200)
        with _host(photo) as host:
            first.show_pdf_page(pymupdf.Rect(0, 100, 400, 200), host, 0)  # photo, through a form
        logo_xref = first.insert_image(pymupdf.Rect(0, 0, 400, 100), stream=logo)
        first.insert_text((10, 160), SECRET, fontsize=30, render_mode=3)
        second = doc.new_page(width=400, height=100)
        second.insert_image(second.rect, xref=logo_xref)
        source = doc.tobytes()
        logo_dark = _dark_pixels(doc, logo_xref)
    output = redact_pdf(source, strategy="email")
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        assert [_dark_pixels(doc, item[0]) for item in doc[1].get_images(full=True)] == [logo_dark]


def test_two_shared_images_in_one_box_keep_their_own_pixels():
    """A photo under a full-page overlay: paired by box alone, the overlay took the
    photo's place on its page and the photo kept the email on the next one."""
    photo = _picture(SECRET)
    with pymupdf.open() as src:  # alike in box, not in size: 600 pixels wide, not 800
        page = src.new_page(width=400, height=100)
        page.insert_text((10, 60), "OVERLAY", fontsize=30)
        overlay = page.get_pixmap(dpi=108).tobytes("png")
    with pymupdf.open() as doc:
        first = doc.new_page(width=400, height=100)
        photo_xref = first.insert_image(first.rect, stream=photo)
        overlay_xref = first.insert_image(first.rect, stream=overlay)
        first.insert_text((10, 60), SECRET, fontsize=30, render_mode=3)
        for xref in (photo_xref, overlay_xref):
            page = doc.new_page(width=400, height=100)
            page.insert_image(page.rect, xref=xref)
        source = doc.tobytes()
    output = redact_pdf(source, strategy="email")
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        widths = [sorted(item[2] for item in page.get_images(full=True)) for page in doc]
        assert widths == [[600, 800], [800], [600]], "each image keeps its place"
        assert all(_dark_pixels(doc, item[0]) == 0 for item in doc[1].get_images(full=True))


def test_marked_content_pikepdf_cannot_read_is_refused_not_a_500():
    """MuPDF draws past a bad hex string; pikepdf cannot rewrite the stream."""
    bad = b"/P <</MCID 0 /ActualText <zz>>> BDC " + SHOWN + b"EMC\n"
    with pytest.raises(ApiError) as refused:
        redact_pdf(_bytes(_pdf(bad + OTHER)), strategy="email")
    assert (refused.value.status_code, refused.value.code) == (422, "content_unreadable")


def _with_form(content: bytes) -> bytes:
    """The plain page plus a form XObject no page draws, holding content."""
    pdf = _pdf()
    form = pdf.make_stream(b"")
    form.write(zlib.compress(content, 9), filter=Name.FlateDecode)
    form.Type, form.Subtype, form.BBox = Name.XObject, Name.Form, Array([0, 0, 10, 10])
    pdf.pages[0].obj.Resources.XObject = Dictionary(Fx=form)
    buf = io.BytesIO()
    pdf.save(buf, stream_decode_level=pikepdf.StreamDecodeLevel.none)
    return buf.getvalue()


def test_a_form_that_inflates_huge_is_read_in_pieces():
    """A 260 KB file held a 256 MiB form; reading it whole cost 386 MiB."""
    source = _with_form(b"%" + b" " * (64 << 20))
    tracemalloc.start()
    try:
        output = redact_pdf(source, strategy="email")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert _copies_left(output, SECRET) == []
    assert peak < 16 << 20


def test_marked_content_too_heavy_to_parse_is_refused():
    marked = b"/P <</MCID 0>> BDC EMC\n"
    with pytest.raises(ApiError) as refused:
        redact_pdf(_with_form(marked * ((9 << 20) // len(marked))), strategy="email")
    assert (refused.value.status_code, refused.value.code) == (422, "content_too_complex")


def test_an_image_drawn_twice_on_the_redacted_page_loses_the_pixels_everywhere():
    """get_images lists an image once per place; only the last place was kept."""
    with pymupdf.open() as doc:
        for _ in range(2):
            doc.new_page(width=400, height=200)
        photo = doc[0].insert_image(pymupdf.Rect(0, 0, 400, 100), stream=_picture(SECRET))
        doc[0].insert_image(pymupdf.Rect(0, 100, 400, 200), xref=photo)
        doc[0].draw_rect(pymupdf.Rect(0, 100, 400, 200), color=None, fill=(1, 1, 1))
        doc[0].insert_text((10, 60), SECRET, fontsize=30, render_mode=3)
        doc[1].insert_image(pymupdf.Rect(0, 0, 400, 100), xref=photo)
        source = doc.tobytes()
    output = redact_pdf(source, strategy="email")
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        images = {item[0] for page in doc for item in page.get_images(full=True)}
        assert {image: _dark_pixels(doc, image) for image in images} == dict.fromkeys(images, 0)


def _dark_on_page(doc, number: int) -> int:
    pix = doc[number].get_pixmap()
    return sum(1 for i in range(0, len(pix.samples), pix.n) if pix.samples[i] < 100)


def test_a_shared_image_wholly_inside_the_box_is_drawn_by_no_page():
    """MuPDF removes it from the redacted page with no copy; the next page showed it whole."""
    with pymupdf.open() as doc:
        for _ in range(2):
            doc.new_page(width=400, height=100)
        photo = doc[0].insert_image(pymupdf.Rect(40, 40, 120, 60), stream=_picture(SECRET))
        doc[0].insert_text((10, 60), SECRET, fontsize=30, render_mode=3)
        doc[1].insert_image(doc[1].rect, xref=photo)
        source = doc.tobytes()
        assert _dark_on_page(doc, 1)
    output = redact_pdf(source, strategy="email")
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        assert _dark_on_page(doc, 1) == 0


def test_two_shared_images_alike_in_box_and_size_are_refused_before_payment():
    """Nothing tells their copies apart: drawn by no page, the logo the email was
    not in vanished from the next page."""
    with pymupdf.open() as doc:
        for _ in range(3):
            doc.new_page(width=400, height=100)
        first = doc[0]
        photos = [first.insert_image(first.rect, stream=_picture(t)) for t in (SECRET, "LOGO")]
        first.insert_text((10, 60), SECRET, fontsize=30, render_mode=3)
        for page, photo in zip(doc.pages(1), photos, strict=True):
            page.insert_image(page.rect, xref=photo)
        source = doc.tobytes()
    with pytest.raises(ApiError) as refused:
        _preview(source)
    assert (refused.value.status_code, refused.value.code) == (422, "images_alike")


def _redacted_in_two_places(*, next_page: bool) -> bytes:
    """An image with two emails, drawn twice on page 0, each place redacted
    over a different one; with next_page, page 1 draws it too."""
    with pymupdf.open() as src:
        page = src.new_page(width=400, height=100)
        page.insert_text((10, 30), SECRET, fontsize=20)
        page.insert_text((150, 80), SECRET, fontsize=20)
        photo = page.get_pixmap(dpi=144).tobytes("png")
    with pymupdf.open() as doc:
        for _ in range(2 if next_page else 1):
            doc.new_page(width=400, height=200)
        first = doc[0]
        xref = first.insert_image(pymupdf.Rect(0, 0, 400, 100), stream=photo)
        first.insert_image(pymupdf.Rect(0, 100, 400, 200), xref=xref)
        first.insert_text((10, 30), SECRET, fontsize=20, render_mode=3)
        first.insert_text((150, 180), SECRET, fontsize=20, render_mode=3)
        if next_page:
            doc[1].insert_image(pymupdf.Rect(0, 0, 400, 100), xref=xref)
        return doc.tobytes()


def test_an_image_redacted_in_two_places_on_one_page_keeps_both_blanks():
    """Two copies, each blank in its own box: written back in turn, the second undid
    the first, and the email came back under the black box. Each copy keeps the
    other box's pixels, as MuPDF made them."""
    output = redact_pdf(_redacted_in_two_places(next_page=False), strategy="email")
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        first = doc[0]
        on_top = [i for i in first.get_images(full=True) if first.get_image_bbox(i).y0 < 50]
        assert on_top and _dark_pixels_in_rows(doc, on_top[0][0], 100) == 0


def test_an_image_redacted_in_two_places_and_shown_elsewhere_is_refused_before_payment():
    """Neither copy holds both blanks, so the next page could show neither: the
    image went from it whole — a logo beside the email too."""
    with pytest.raises(ApiError) as refused:
        _preview(_redacted_in_two_places(next_page=True))
    assert (refused.value.status_code, refused.value.code) == (422, "image_redacted_twice")


def _dark_pixels_in_rows(doc, xref: int, rows: int) -> int:
    pix = pymupdf.Pixmap(doc, xref)
    rows = min(rows, pix.height)
    return sum(1 for i in range(0, rows * pix.width * pix.n, pix.n) if pix.samples[i] < 100)


def test_an_image_a_page_only_lists_is_not_taken_for_a_redacted_one():
    """Pages sharing one resource dictionary list each other's images; the cleanup
    after a redaction dropped them, and a logo vanished from every page."""
    with pymupdf.open() as doc:
        for _ in range(2):
            doc.new_page(width=400, height=100)
        doc[0].insert_text((10, 60), SECRET, fontsize=30)
        doc[1].insert_image(doc[1].rect, stream=_picture("LOGO"))
        logo_dark = _dark_on_page(doc, 1)
        built = doc.tobytes()
    with pikepdf.open(io.BytesIO(built)) as pdf:
        first, second = (page.obj.Resources for page in pdf.pages)
        shared = pdf.make_indirect(Dictionary(Font=first.Font, XObject=second.XObject))
        for page in pdf.pages:
            page.obj.Resources = shared
        source = _bytes(pdf)
    output = redact_pdf(source, strategy="email")
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        assert _copies_left(output, SECRET) == []
        assert _dark_on_page(doc, 1) == logo_dark > 0


def test_an_inline_image_in_a_form_two_pages_draw_is_refused_under_a_match():
    """MuPDF blanks the pixels in a copy of the form for its page; the other page
    kept the original form, the email intact in its image."""
    pdf = _pdf(pages=2, content=b"")
    with pymupdf.open() as src:
        page = src.new_page(width=40, height=10)
        page.insert_text((1, 8), SECRET, fontsize=4)
        pix = page.get_pixmap(colorspace=pymupdf.csGRAY)
    image = (b"q 400 0 0 100 0 0 cm BI /W %d /H %d /BPC 8 /CS /G ID " % (pix.width, pix.height)
             + pix.samples + b" EI Q")
    form = pdf.make_stream(image)
    form.Type, form.Subtype, form.BBox = Name.XObject, Name.Form, Array([0, 0, 400, 100])
    for page in pdf.pages:
        page.obj.Resources.XObject = Dictionary(Fx=form)
        page.obj.Contents = pdf.make_stream(b"/Fx Do\n")
    pdf.pages[0].obj.Contents = pdf.make_stream(
        b"/Fx Do\nBT 3 Tr /F1 40 Tf 10 30 Td (ana@example.com) Tj ET\n")
    with pytest.raises(ApiError) as refused:
        redact_pdf(_bytes(pdf), strategy="email")
    assert (refused.value.status_code, refused.value.code) == (422, "image_in_shared_form")


def test_a_content_stream_that_fails_to_decode_is_refused_not_a_500():
    pdf = _pdf()
    form = pdf.make_stream(b"")
    form.write(b"\x78\x9c not deflate data", filter=Name.FlateDecode,
               decode_parms=Dictionary(Predictor=12, Columns=4))
    form.Type, form.Subtype, form.BBox = Name.XObject, Name.Form, Array([0, 0, 10, 10])
    pdf.pages[0].obj.Resources.XObject = Dictionary(Fx=form)
    buf = io.BytesIO()
    pdf.save(buf, stream_decode_level=pikepdf.StreamDecodeLevel.none)
    with pytest.raises(ApiError) as refused:
        redact_pdf(buf.getvalue(), strategy="email")
    assert (refused.value.status_code, refused.value.code) == (422, "content_unreadable")


@pytest.mark.parametrize("build, code", [
    (lambda: _bytes(_pdf(b"/P <</MCID 0 /ActualText <zz>>> BDC " + SHOWN + b"EMC\n" + OTHER)),
     "content_unreadable"),
    (lambda: _with_form(b"/P <</MCID 0>> BDC EMC\n" * ((9 << 20) // 23)), "content_too_complex"),
], ids=["unreadable", "too-heavy"])
def test_what_the_apply_refuses_the_preview_refuses_before_payment(build, code):
    from app.router_v2 import _extract_matches_json

    with pytest.raises(ApiError) as refused:
        _extract_matches_json(build(), strategy="email", custom_text="", regex_pattern="",
                              match_cap=100)
    assert (refused.value.status_code, refused.value.code) == (422, code)


def test_a_shared_letterhead_saying_combined_does_not_refuse_a_page_image():
    """«COMBINED» holds the bytes BI; only the operator means an inline image."""
    pdf = _pdf(pages=2, content=b"")
    letterhead = pdf.make_stream(b"BT /F1 12 Tf 72 760 Td (COMBINED REPORT) Tj ET")
    letterhead.Type, letterhead.Subtype = Name.XObject, Name.Form
    letterhead.BBox, letterhead.Resources = Array([0, 0, 612, 792]), pdf.pages[0].obj.Resources
    image = b"q 400 0 0 100 0 0 cm BI /W 4 /H 1 /BPC 8 /CS /G ID \x00\x80\xc0\xff EI Q\n"
    for page in pdf.pages:
        page.obj.Resources.XObject = Dictionary(Head=letterhead)
        page.obj.Contents = pdf.make_stream(b"/Head Do\n")
    pdf.pages[0].obj.Contents = pdf.make_stream(
        b"/Head Do\n" + image + b"BT 3 Tr /F1 40 Tf 10 30 Td (ana@example.com) Tj ET\n")
    output = redact_pdf(_bytes(pdf), strategy="email")
    assert _copies_left(output, SECRET) == []


def _preview(source: bytes) -> dict:
    from app.router_v2 import _extract_matches_json

    return _extract_matches_json(source, strategy="email", custom_text="", regex_pattern="",
                                 match_cap=100)


def test_marked_content_behind_a_bad_checksum_keeps_no_copy():
    """MuPDF and qpdf read past a wrong Adler-32; the scan stopped there, and the
    stream's /ActualText kept the email."""
    content = b"/P <</MCID 0 /ActualText (ana@example.com)>> BDC " + SHOWN + b"EMC\n" + OTHER
    raw = zlib.compress(content)
    pdf = _pdf()
    pdf.pages[0].obj.Contents.write(raw[:-1] + bytes([raw[-1] ^ 1]), filter=Name.FlateDecode)
    buf = io.BytesIO()
    pdf.save(buf, stream_decode_level=pikepdf.StreamDecodeLevel.none)
    assert _copies_left(redact_pdf(buf.getvalue(), strategy="email"), SECRET) == []


@pytest.mark.parametrize("cell", [
    b"BT /F1 0 Tf 20 20 Td (ana@example.com) Tj ET",
    b"q 0 0 0 0 0 0 cm BT /F1 10 Tf 20 20 Td (ana@example.com) Tj ET Q",
], ids=["zero-size", "singular-matrix"])
def test_a_match_drawn_at_no_size_in_a_fill_pattern_is_refused(cell):
    """Text drawn at no size extracts as nothing, yet the cell holds it."""
    with pikepdf.open(io.BytesIO(_pattern_pdf(b"SAFE"))) as pdf:
        pdf.pages[0].obj.Resources.Pattern.Pt1.write(cell)
        source = _bytes(pdf)
    with pytest.raises(ApiError) as refused:
        _preview(source)
    assert (refused.value.status_code, refused.value.code) == (422, "text_in_fill_pattern")


@pytest.mark.parametrize("hidden", [
    b"BT /F1 0 Tf 20 20 Td (ana@example.com) Tj ET\n",
    b"BT /F1 0 Tf 72 650 Td (ana@example.com) Tj ET\n",  # where another line starts
    b"q 0 0 0 0 0 0 cm BT /F1 10 Tf 20 20 Td (ana@example.com) Tj ET Q\n",
    b"q 1 0 0 0 0 400 cm BT /F1 12 Tf 20 20 Td (ana@example.com) Tj ET Q\n",
], ids=["zero-size", "zero-size-over-other-text", "singular-matrix", "flat-height"])
def test_a_match_drawn_at_no_size_on_a_page_is_refused_before_payment(hidden):
    """No viewer shows it and MuPDF extracts nothing, so no box removed it."""
    with pytest.raises(ApiError) as refused:
        _preview(_bytes(_pdf(SHOWN + hidden + OTHER)))
    assert (refused.value.status_code, refused.value.code) == (422, "text_of_no_size")


def test_unrelated_text_drawn_at_no_size_is_fine():
    output = redact_pdf(_bytes(_pdf(SHOWN + b"BT /F1 0 Tf 20 20 Td (spacer) Tj ET\n" + OTHER)),
                        strategy="email")
    assert _copies_left(output, SECRET) == []


@pytest.mark.parametrize("kind", [Name.OCG, Name.OCMD])
def test_replacement_text_in_a_layer_property_list_keeps_no_copy(kind):
    pdf = _pdf(b"/Span /pl1 BDC " + SHOWN + b"EMC\n" + OTHER)
    layer = pdf.make_indirect(
        Dictionary(Type=kind, Name=String("normal"), ActualText=String(SECRET)))
    pdf.pages[0].obj.Resources.Properties = Dictionary(pl1=layer)
    output = _redacted(pdf)
    assert _copies_left(output, SECRET) == []
    assert SECRET not in _drawn_text(output)


def _picture_twice(*, cover: pymupdf.Rect, hidden_at: tuple, pages: int):
    """One image drawn twice on page 0 under ONE resource name, a white box over
    one place, the email as invisible text over the other."""
    with pymupdf.open() as doc:
        for _ in range(pages):
            doc.new_page(width=400, height=200)
        xref = doc[0].insert_image(pymupdf.Rect(0, 0, 400, 100), stream=_picture(SECRET))
        doc[0].insert_image(pymupdf.Rect(0, 100, 400, 200), xref=xref)
        doc[0].draw_rect(cover, color=None, fill=(1, 1, 1))
        doc[0].insert_text(hidden_at, SECRET, fontsize=30, render_mode=3)
        if pages > 1:
            doc[1].insert_image(pymupdf.Rect(0, 0, 400, 100), xref=xref)
        built = doc.tobytes()
    with pikepdf.open(io.BytesIO(built)) as pdf:
        page = pdf.pages[0].obj
        first, second = list(page.Resources.XObject.keys())
        content = b"".join(part.read_bytes() for part in page.Contents)
        page.Contents = pdf.make_stream(content.replace(second.encode(), first.encode()))
        del page.Resources.XObject[second]
        return _bytes(pdf)


@pytest.mark.parametrize("cover, hidden_at, pages", [
    (pymupdf.Rect(0, 0, 400, 100), (10, 160), 2),
    (pymupdf.Rect(0, 100, 400, 200), (10, 60), 1),
], ids=["shared-with-a-page", "this-page-only"])
def test_an_image_drawn_twice_under_one_name_loses_the_pixels_everywhere(cover, hidden_at, pages):
    """get_images lists it once, at its first place: the other place, under a white
    box or on the next page, kept the email."""
    output = redact_pdf(_picture_twice(cover=cover, hidden_at=hidden_at, pages=pages),
                        strategy="email")
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        images = {item[0] for page in doc for item in page.get_images(full=True)}
        assert {image: _dark_pixels(doc, image) for image in images} == dict.fromkeys(images, 0)


def test_a_pending_mark_on_a_shared_image_blanks_it_on_every_page():
    """Another editor's /Redact mark blanked a copy for its page; the next page
    drawing the image kept the email."""
    with pymupdf.open() as doc:
        for _ in range(2):
            doc.new_page(width=400, height=200)
        xref = doc[0].insert_image(pymupdf.Rect(0, 0, 400, 100), stream=_picture(SECRET))
        doc[1].insert_image(pymupdf.Rect(0, 0, 400, 100), xref=xref)
        doc[0].add_redact_annot(pymupdf.Rect(0, 30, 400, 70), fill=(0, 0, 0))
        source = doc.tobytes()
        assert _dark_on_page(doc, 1)
    with pymupdf.open(stream=redact_pdf(source, strategy="email"), filetype="pdf") as doc:
        assert _dark_on_page(doc, 1) == 0


def test_a_shared_letterhead_saying_bi_does_not_refuse_a_page_image():
    """«(Monthly BI Report)» holds BI between spaces, inside a string."""
    pdf = _pdf(pages=2, content=b"")
    letterhead = pdf.make_stream(b"BT /F1 10 Tf 10 10 Td (Monthly BI Report) Tj ET")
    letterhead.Type, letterhead.Subtype = Name.XObject, Name.Form
    letterhead.BBox, letterhead.Resources = Array([0, 0, 400, 100]), pdf.pages[0].obj.Resources
    for page in pdf.pages:
        page.obj.Resources.XObject = Dictionary(Fx=letterhead)
        page.obj.Contents = pdf.make_stream(b"q 1 0 0 1 0 200 cm /Fx Do Q")
    pdf.pages[0].obj.Contents = pdf.make_stream(
        b"q 400 0 0 100 0 0 cm BI /W 1 /H 1 /BPC 8 /CS /G ID \xff EI Q\n"
        b"BT 3 Tr /F1 40 Tf 10 30 Td (ana@example.com) Tj ET\nq 1 0 0 1 0 200 cm /Fx Do Q")
    assert _copies_left(_redacted(pdf), SECRET) == []


@pytest.mark.parametrize("operator", [b"BDC", b"DP"])
def test_marked_content_with_no_operands_is_not_a_500(operator):
    source = _bytes(_pdf(b"BDC\n" + SHOWN + b"EMC\n" + OTHER + b"/P <<>> DP\n" + operator + b"\n"))
    assert _preview(source)["total"] == 1
    assert _copies_left(redact_pdf(source, strategy="email"), SECRET) == []


def test_layer_configurations_lose_the_match():
    pdf = _pdf()
    layer = pdf.make_indirect(Dictionary(Type=Name.OCG, Name=String("normal")))
    pdf.Root.OCProperties = Dictionary(
        OCGs=Array([layer]),
        D=Dictionary(Name=String("Review by " + SECRET), Creator=String(SECRET), ON=Array([layer])),
        Configs=Array([Dictionary(Name=String("Print for " + SECRET), BaseState=Name.ON)]),
    )
    output = _redacted(pdf)
    assert _copies_left(output, SECRET) == []
    with pikepdf.open(io.BytesIO(output)) as result:
        assert str(result.Root.OCProperties.D.Name) == "Review by "


@pytest.mark.parametrize("ocr_twin", [None, (51, 201), (20, 201)],
                         ids=["digital", "digital-run-through-ocr", "wide-hidden-span"])
def test_a_shared_background_under_visible_text_stays_whole_on_other_pages(ocr_twin):
    """The email is the text, not the background's pixels: blanked on every page,
    the next page's background got a hole where page 1's email was. OCR run on a
    digital page lays an invisible twin over each visible word."""
    background = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200), False)
    background.set_rect(background.irect, (200, 220, 255))
    with pymupdf.open() as doc:
        for _ in range(2):
            doc.new_page(width=400, height=400)
        xref = doc[0].insert_image(doc[0].rect, pixmap=background)
        doc[1].insert_image(doc[1].rect, xref=xref)
        doc[0].insert_text((50, 200), SECRET, fontsize=20)
        if ocr_twin:  # the twin, or «A», 54 spaces and «B» across the email
            hidden = SECRET if ocr_twin[0] > 50 else "A" + " " * 54 + "B"
            doc[0].insert_text(ocr_twin, hidden, fontsize=20, render_mode=3)
        source = doc.tobytes()

    def next_page(pdf: bytes) -> bytes:
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            return doc[1].get_pixmap(dpi=36).samples

    output = redact_pdf(source, strategy="email")
    assert next_page(output) == next_page(source)
    assert _copies_left(output, SECRET) == []


def test_two_objects_with_the_same_pixels_are_not_taken_for_alike():
    """get_image_rects finds images by their pixels: each copy claimed both places."""
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=200)
        for y in (0, 100):
            page.insert_image(pymupdf.Rect(0, y, 400, y + 100), stream=_picture(SECRET))
            page.insert_text((10, y + 60), SECRET, fontsize=30, render_mode=3)
        built = doc.tobytes()
    with pikepdf.open(io.BytesIO(built)) as pdf:  # PyMuPDF stored the picture once
        images = pdf.pages[0].Resources.XObject
        first, second = list(images.keys())
        twin = pdf.make_stream(images[first].read_raw_bytes())
        for key, value in images[first].items():
            if key != "/Length":
                twin[key] = value
        images[second] = twin
        source = _bytes(pdf)
    assert _preview(source)["total"] == 2
    with pymupdf.open(stream=redact_pdf(source, strategy="email"), filetype="pdf") as doc:
        images = [item[0] for item in doc[0].get_images(full=True)]
        assert len(images) == 2 and [_dark_pixels(doc, image) for image in images] == [0, 0]


def test_visible_text_over_a_local_inline_image_with_a_shared_inline_logo_is_fine():
    """Only a match over hidden text can be in an image's pixels."""
    pdf = _pdf(pages=2, content=b"")
    inline = b"BI /W 1 /H 1 /BPC 8 /CS /G ID \xff EI\n"
    header = pdf.make_stream(b"q 20 0 0 20 550 740 cm " + inline + b"Q\n")
    header.Type, header.Subtype, header.BBox = Name.XObject, Name.Form, Array([0, 0, 612, 792])
    for page in pdf.pages:
        page.obj.Resources.XObject = Dictionary(Head=header)
        page.obj.Contents = pdf.make_stream(b"/Head Do\n")
    pdf.pages[0].obj.Contents = pdf.make_stream(
        b"/Head Do\nq 500 0 0 100 50 650 cm " + inline + b"Q\n" + SHOWN + OTHER)
    assert _copies_left(_redacted(pdf), SECRET) == []


@pytest.mark.parametrize("mode", [b"3", b"0"], ids=["hidden-text", "visible-text"])
def test_an_image_too_large_to_decode_is_refused_at_preview(mode):
    """Finding where images are drawn decodes them: a 20 KB file declared 156 Mpx,
    in a preview that costs no free use. The apply refused it, after payment."""
    side = 12_500
    pdf = _pdf(b"q 612 0 0 792 0 0 cm /Scan Do Q\n"
               b"BT " + mode + b" Tr /F1 12 Tf 72 700 Td (ana@example.com) Tj ET\n")
    scan = pdf.make_stream(b"")
    scan.write(zlib.compress(bytes(side * side // 8), 9), filter=Name.FlateDecode)
    scan.Type, scan.Subtype, scan.Width, scan.Height = Name.XObject, Name.Image, side, side
    scan.ColorSpace, scan.BitsPerComponent = Name.DeviceGray, 1
    pdf.pages[0].obj.Resources.XObject = Dictionary(Scan=scan)
    buf = io.BytesIO()
    pdf.save(buf, stream_decode_level=pikepdf.StreamDecodeLevel.none)
    with pytest.raises(ApiError) as refused:
        _preview(buf.getvalue())
    assert (refused.value.status_code, refused.value.code) == (422, "image_too_large")


def test_an_accented_match_in_capitals_is_searched_on_its_own():
    """search_for ignores ASCII case only: «JOÃO» collapsed into «joão» was never
    searched, and stayed."""
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), "joão@exemplo.pt", fontsize=12)
        page.insert_text((72, 150), "JOÃO@EXEMPLO.PT", fontsize=12)
        source = doc.tobytes()
    assert _preview(source)["total"] == 2
    with pymupdf.open(stream=redact_pdf(source, strategy="email"), filetype="pdf") as doc:
        assert "@" not in doc[0].get_text()


def test_a_structure_array_holding_itself_ends():
    pdf = _tagged({})
    loop = pdf.make_indirect(Array([0]))
    loop.append(loop)
    pdf.Root.StructTreeRoot.K[0].K = loop
    assert _copies_left(redact_pdf(_bytes(pdf), strategy="email"), SECRET) == []


def test_an_article_title_and_author_lose_the_match():
    pdf = _pdf()
    bead = pdf.make_indirect(Dictionary(P=pdf.pages[0].obj, R=Array([72, 600, 300, 720])))
    thread = pdf.make_indirect(Dictionary(
        F=bead, I=Dictionary(Title=String("Notes for " + SECRET), Author=String(SECRET))))
    bead.T, bead.N, bead.V = thread, bead, bead
    pdf.Root.Threads = Array([thread])
    pdf.pages[0].obj.B = Array([bead])
    assert _copies_left(_redacted(pdf), SECRET) == []


def test_application_data_on_an_image_keeps_no_copy():
    pdf = _pdf()
    image = pdf.make_stream(b"\xff", Type=Name.XObject, Subtype=Name.Image, Width=1, Height=1,
                            BitsPerComponent=8, ColorSpace=Name.DeviceGray)
    image.PieceInfo = Dictionary(Illustrator=Dictionary(
        LastModified=String("D:20261003"), Private=Dictionary(Note=String(SECRET))))
    pdf.pages[0].obj.Resources.XObject = Dictionary(Im1=image)
    pdf.pages[0].obj.Contents = pdf.make_stream(SHOWN + OTHER + b"q 9 0 0 9 9 9 cm /Im1 Do Q\n")
    assert _copies_left(_redacted(pdf), SECRET) == []


def _scan(text: str, at: tuple) -> bytes:
    """A scanned page: dark text on a coloured ground, where a blank shows."""
    with pymupdf.open() as src:
        page = src.new_page(width=400, height=400)
        page.draw_rect(page.rect, fill=(0.78, 0.86, 1), color=None)
        page.insert_text(at, text, fontsize=30)
        return page.get_pixmap().tobytes("png")


def _shown(pdf: bytes, number: int) -> bytes:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return doc[number].get_pixmap(dpi=36).samples


def _scan_on_two_pages(first_page) -> tuple[bytes, bytes]:
    """A scan both pages draw, OCR'd on the first; the second shows its lower part."""
    with pymupdf.open() as doc:
        for _ in range(2):
            doc.new_page(width=400, height=400)
        xref = doc[0].insert_image(doc[0].rect, stream=_scan(SECRET, (50, 60)))
        doc[1].insert_image(doc[1].rect, xref=xref)
        doc[0].insert_text((50, 60), SECRET, fontsize=30, render_mode=3)  # its OCR layer
        first_page(doc[0])
        doc[1].set_cropbox(pymupdf.Rect(0, 100, 400, 400))
        source = doc.tobytes()
    return source, redact_pdf(source, strategy="email")


def test_an_ocr_match_between_spaced_out_labels_leaves_no_copy_in_a_shared_scan():
    """Labels in columns are one span with spaces between them: its box took the
    OCR'd email for visible text, and the next page's scan kept the email."""
    _, output = _scan_on_two_pages(
        lambda page: page.insert_text((1, 60), "Ref:" + " " * 90 + "End", fontsize=12))
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        assert _dark_pixels(doc, doc[1].get_images(full=True)[0][0]) == 0


def test_visible_text_beside_an_ocr_match_leaves_no_hole_in_a_shared_scan():
    """Blanked under every box, the scan went back to the next page with a hole
    where page 1 had typed text over it."""
    source, output = _scan_on_two_pages(
        lambda page: page.insert_text((40, 250), "bob@example.net", fontsize=20))
    assert _shown(output, 1) == _shown(source, 1)
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        assert _dark_pixels(doc, doc[1].get_images(full=True)[0][0]) == 0
    assert _copies_left(output, "bob@example.net") == []


def _background_on_two_pages():
    background = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200), False)
    background.set_rect(background.irect, (200, 220, 255))
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=400, height=400)
    doc[1].insert_image(doc[1].rect, xref=doc[0].insert_image(doc[0].rect, pixmap=background))
    return doc


@pytest.mark.parametrize("image", ["background", "scan"])
def test_a_pending_mark_over_visible_text_and_a_shared_image_is_refused_before_payment(image):
    """The image may hold what the mark covers (a scan: blank it everywhere) or
    be a background (blanked everywhere, a hole on every other page)."""
    if image == "scan":
        doc = pymupdf.open()
        for _ in range(2):
            doc.new_page(width=400, height=400)
        xref = doc[0].insert_image(doc[0].rect, stream=_scan(SECRET, (50, 60)))
        doc[1].insert_image(doc[1].rect, xref=xref)
    else:
        doc = _background_on_two_pages()
    with doc:
        doc[0].insert_text((50, 90), "Caption", fontsize=12)
        doc[0].add_redact_annot(pymupdf.Rect(45, 30, 320, 95), fill=(0, 0, 0))
        source = doc.tobytes()
    with pytest.raises(ApiError) as refused:
        _preview(source)
    assert (refused.value.status_code, refused.value.code) == (422, "mark_over_shared_image")


def test_a_pending_mark_over_visible_text_alone_is_applied():
    with pymupdf.open() as doc:
        doc.new_page(width=400, height=400).insert_text((50, 200), "Private note", fontsize=20)
        doc[0].add_redact_annot(pymupdf.Rect(45, 180, 200, 210), fill=(0, 0, 0))
        source = doc.tobytes()
    assert "Private" not in _drawn_text(redact_pdf(source, strategy="email"))


def test_a_match_found_through_a_lookbehind_on_the_line_above_is_redacted():
    """Judged on its own line, the hit lost the context the pattern matched
    with; the copy inside «ana@example.com.pt» never matched."""
    content = (b"BT /F1 12 Tf 72 700 Td (Email:) Tj ET\n"
               b"BT /F1 12 Tf 72 680 Td (ana@example.com) Tj ET\n"
               b"BT /F1 12 Tf 72 640 Td (Copy ana@example.com.pt) Tj ET\n")
    source = _bytes(_pdf(content))
    kwargs = {"strategy": "regex", "custom_text": "", "regex_pattern": r"(?<=Email:\n)\S+"}
    from app.router_v2 import _extract_matches_json
    assert _extract_matches_json(source, match_cap=100, **kwargs)["total"] == 1
    text = _drawn_text(redact_pdf(source, strategy="regex", regex_pattern=kwargs["regex_pattern"]))
    assert text == "Email:\nCopy ana@example.com.pt\n"


@pytest.mark.parametrize("lines", [
    [b"Tel 912345678", b"Conta 19123456780"],
    [b"Tel 912345678 Conta 19123456780"],
    [b"Tel 912345678, 19123456780"],
    [b"Tel (912345678) 19123456780"],
], ids=["two-lines", "one-line", "comma", "brackets"])
def test_a_phone_inside_a_longer_figure_keeps_the_figure(lines):
    """search_for found 912345678 inside the account 19123456780 too."""
    content = b"".join(b"BT /F1 12 Tf 72 %d Td (%s) Tj ET\n" % (700 - 20 * i, line)
                       for i, line in enumerate(lines))
    text = _drawn_text(redact_pdf(_bytes(_pdf(content)), strategy="phone"))
    assert "19123456780" in text
    assert "912345678" not in text.replace("19123456780", "")


def test_a_match_split_across_fonts_does_not_hide_a_copy_at_no_size():
    """Counted, the size-0 copy stood in for the split one the trace did not join."""
    pdf = _pdf(b"BT /F1 12 Tf 72 700 Td (ana@exam) Tj /F2 12 Tf (ple.com) Tj ET\n"
               b"BT /F1 0 Tf 20 20 Td (ana@example.com) Tj ET\n" + OTHER)
    pdf.pages[0].obj.Resources.Font.F2 = Dictionary(
        Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Courier)
    with pytest.raises(ApiError) as refused:
        _preview(_bytes(pdf))
    assert (refused.value.status_code, refused.value.code) == (422, "text_of_no_size")


def test_an_element_only_the_parent_tree_reaches_loses_the_match():
    pdf = _tagged({})
    root = pdf.Root.StructTreeRoot
    orphan = pdf.make_indirect(Dictionary(
        S=Name.Figure, P=root, Pg=pdf.pages[0].obj, K=2, Alt=String("Photo of " + SECRET)))
    root.ParentTree.Nums[1].append(orphan)
    output = _redacted(pdf)
    assert _copies_left(output, SECRET) == []


def test_an_article_whose_information_is_not_a_dictionary_is_not_a_500():
    pdf = _pdf()
    bead = pdf.make_indirect(Dictionary(P=pdf.pages[0].obj, R=Array([72, 600, 300, 720])))
    thread = pdf.make_indirect(Dictionary(F=bead, I=String("Notes")))
    bead.T, bead.N, bead.V = thread, bead, bead
    pdf.Root.Threads = Array([thread])
    pdf.pages[0].obj.B = Array([bead])
    assert _preview(_bytes(pdf))["total"] == 1
    assert _copies_left(_redacted(pdf), SECRET) == []


def test_a_phrase_across_two_lines_is_redacted_on_both():
    """Each line holds part of the hit: judged alone, neither was the phrase,
    and «Ana Silvano», where the pattern does not match, lost «Ana Silva»."""
    content = (b"BT /F1 12 Tf 72 700 Td (Contact Ana) Tj ET\n"
               b"BT /F1 12 Tf 72 682 Td (Silva today) Tj ET\n"
               b"BT /F1 12 Tf 72 640 Td (Signed Ana Silvano) Tj ET\n")
    output = redact_pdf(_bytes(_pdf(content)), strategy="regex",
                        regex_pattern=r"\bAna\s+Silva\b")
    text = _drawn_text(output)
    assert "Contact" in text and "today" in text and "Signed Ana Silvano" in text
    assert "Ana\n" not in text and "Silva " not in text


def test_a_phone_drawn_twice_over_itself_is_redacted():
    """A filled field drawn over itself reads «912345678 912345678» on one line,
    where the pattern finds neither."""
    content = (b"BT /F1 12 Tf 72 700 Td (912345678) Tj ET\n"
               b"BT /F1 12 Tf 72.4 700 Td (912345678) Tj ET\n")
    assert "912345678" not in _drawn_text(redact_pdf(_bytes(_pdf(content)), strategy="phone"))


def test_an_article_whose_information_is_a_string_loses_the_match():
    pdf = _pdf()
    bead = pdf.make_indirect(Dictionary(P=pdf.pages[0].obj, R=Array([72, 600, 300, 720])))
    thread = pdf.make_indirect(Dictionary(F=bead, I=String("Notes for " + SECRET)))
    bead.T, bead.N, bead.V = thread, bead, bead
    pdf.Root.Threads = Array([thread])
    pdf.pages[0].obj.B = Array([bead])
    assert _copies_left(_redacted(pdf), SECRET) == []


def test_a_pending_mark_over_an_image_another_page_only_lists_is_applied():
    """Pages sharing one resource dictionary each list every page's images."""
    with pymupdf.open() as doc:
        for _ in range(2):
            doc.new_page(width=400, height=400)
        doc[0].insert_image(doc[0].rect, stream=_scan(SECRET, (50, 60)))
        doc[0].insert_text((50, 90), "Caption", fontsize=12)
        doc.xref_set_key(doc[1].xref, "Resources", doc.xref_get_key(doc[0].xref, "Resources")[1])
        assert doc[1].get_images() and not doc[1].get_image_info()
        doc[0].add_redact_annot(pymupdf.Rect(45, 30, 320, 95), fill=(0, 0, 0))
        source = doc.tobytes()
    assert "Caption" not in _drawn_text(redact_pdf(source, strategy="email"))


def test_an_article_whose_information_is_an_array_loses_the_match():
    pdf = _pdf()
    bead = pdf.make_indirect(Dictionary(P=pdf.pages[0].obj, R=Array([72, 600, 300, 720])))
    thread = pdf.make_indirect(Dictionary(F=bead, I=Array([String("Notes for " + SECRET)])))
    bead.T, bead.N, bead.V = thread, bead, bead
    pdf.Root.Threads = Array([thread])
    pdf.pages[0].obj.B = Array([bead])
    assert _copies_left(_redacted(pdf), SECRET) == []


def test_a_phone_drawn_twice_in_one_place_does_not_cost_a_longer_figure_its_digits():
    """Two matches, two hits — one of them inside the account: counted, they agreed."""
    content = (b"BT /F1 12 Tf 72 700 Td (Tel 912345678) Tj ET\n"
               b"BT /F1 12 Tf 72 700 Td (Tel 912345678) Tj ET\n"
               b"BT /F1 12 Tf 72 680 Td (Conta 19123456780) Tj ET\n")
    text = _drawn_text(redact_pdf(_bytes(_pdf(content)), strategy="phone"))
    assert "19123456780" in text
    assert "912345678" not in text.replace("19123456780", "")


@pytest.mark.parametrize("twins", [(85,), (50, 85)], ids=["account", "every-line"])
def test_an_ocr_twin_over_a_longer_figure_keeps_the_figure(twins):
    """An OCR'd digital page draws hidden text over its own: one hit held both
    copies, and the account 19123456780 lost its digits."""
    lines = {50: "Tel 912345678", 85: "Conta 19123456780"}
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=200)
        for y, line in lines.items():
            page.insert_text((50, y), line, fontsize=12)
        for y in twins:
            page.insert_text((50, y), lines[y], fontsize=12, render_mode=3)
        account = page.get_pixmap(clip=(40, 70, 220, 100)).digest
        source = doc.tobytes()
    with pymupdf.open(stream=redact_pdf(source, strategy="phone"), filetype="pdf") as doc:
        assert doc[0].get_pixmap(clip=(40, 70, 220, 100)).digest == account
        assert "912345678" not in doc[0].get_text().replace("19123456780", "")


@pytest.mark.parametrize("hidden_on", [1, 0], ids=["other-page", "same-page"])
def test_a_pending_mark_over_a_shared_image_drawn_in_a_layer_that_is_off_is_refused(hidden_on):
    """A layer that is off draws nothing, yet any reader switches it on: there
    the scan kept what the mark covered."""
    with pymupdf.open() as doc:
        for _ in range(2):
            doc.new_page(width=400, height=400)
        xref = doc[0].insert_image(doc[0].rect, stream=_scan(SECRET, (50, 60)))
        doc[hidden_on].insert_image(pymupdf.Rect(200, 200, 400, 400), xref=xref)
        layer = doc.add_ocg("Hidden copy", on=False)
        page = doc[hidden_on]
        resources = int(doc.xref_get_key(page.xref, "Resources")[1].split()[0])
        doc.xref_set_key(resources, "Properties", f"<< /H {layer} 0 R >>")
        last = page.get_contents()[-1]
        doc.update_stream(last, b"/OC /H BDC\n" + doc.xref_stream(last) + b"\nEMC")
        doc[0].insert_text((50, 90), "Caption", fontsize=12)
        doc[0].add_redact_annot(pymupdf.Rect(45, 30, 320, 95), fill=(0, 0, 0))
        source = doc.tobytes()
    with pytest.raises(ApiError) as refused:
        _preview(source)
    assert (refused.value.status_code, refused.value.code) == (422, "mark_over_shared_image")


@pytest.mark.parametrize("cover", ["mark", "ocr"])
def test_a_scan_drawn_again_on_its_page_in_a_layer_that_is_off_keeps_no_original(cover):
    """apply_redactions leaves what a layer that is off draws: switched on, the
    second placement showed the scan as it was."""
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=400)
        xref = page.insert_image(page.rect, stream=_scan(SECRET, (50, 60)))
        original = pymupdf.Pixmap(doc, xref).digest
        page.insert_image(pymupdf.Rect(200, 200, 400, 400), xref=xref)
        layer = doc.add_ocg("Hidden copy", on=False)
        resources = int(doc.xref_get_key(page.xref, "Resources")[1].split()[0])
        doc.xref_set_key(resources, "Properties", f"<< /H {layer} 0 R >>")
        last = page.get_contents()[-1]
        doc.update_stream(last, b"/OC /H BDC\n" + doc.xref_stream(last) + b"\nEMC")
        if cover == "mark":
            page.add_redact_annot(pymupdf.Rect(45, 30, 320, 95), fill=(0, 0, 0))
        else:
            page.insert_text((50, 60), SECRET, fontsize=30, render_mode=3)  # its OCR layer
        source = doc.tobytes()
    with pymupdf.open(stream=source, filetype="pdf") as doc:
        assert len(doc[0].get_image_info()) == 1  # MuPDF reads layers on open
    with pymupdf.open(stream=redact_pdf(source, strategy="email"), filetype="pdf") as doc:
        assert [pymupdf.Pixmap(doc, item[0]).digest for item in doc[0].get_images(full=True)
                if pymupdf.Pixmap(doc, item[0]).digest == original] == []


def test_a_pending_mark_over_an_image_the_last_page_draws_twice_is_not_a_500():
    """The loop over the pages still held the last one when the redaction
    reloaded it."""
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=400)
        xref = page.insert_image(page.rect, stream=_scan(SECRET, (50, 60)))
        page.insert_image(pymupdf.Rect(200, 200, 400, 400), xref=xref)
        original = pymupdf.Pixmap(doc, xref).digest
        page.add_redact_annot(pymupdf.Rect(45, 30, 320, 95), fill=(0, 0, 0))
        source = doc.tobytes()
    with pymupdf.open(stream=redact_pdf(source, strategy="email"), filetype="pdf") as doc:
        assert all(pymupdf.Pixmap(doc, item[0]).digest != original
                   for item in doc[0].get_images(full=True))


def test_a_hit_inside_another_match_is_not_taken_for_one():
    """«Silvano» matched, and «Ana Silva» found inside «Ana Silvano» stood on it:
    «Ana» went, where nothing matched."""
    content = (b"BT /F1 12 Tf 72 700 Td (Contact Ana Silva) Tj ET\n"
               b"BT /F1 12 Tf 72 660 Td (Signer Ana Silvano) Tj ET\n")
    output = redact_pdf(_bytes(_pdf(content)), strategy="regex",
                        regex_pattern=r"\bAna\s+Silva\b|\bSilvano\b")
    text = _drawn_text(output)
    assert "Contact" in text and "Signer Ana" in text
    assert "Silva" not in text


def test_a_full_stop_kerned_under_the_last_letter_does_not_unmake_the_match():
    """A font kerns «V.»: the full stop's centre fell inside the hit, and a
    copy the match did not hold every glyph of stood for nothing."""
    content = b"BT /F1 12 Tf 72 700 Td [(Contact ana@example.TV) 400 (.)] TJ ET\n" + OTHER
    source = _bytes(_pdf(content))
    assert _preview(source)["total"] == 1
    assert "ana@example" not in _drawn_text(redact_pdf(source, strategy="email"))
