"""Regressions found by the audit's final review (2026-09-24), ids P1-1 … P2-5.

Each test failed on b0687b4, the audit's last deploy, unless its docstring
calls it a guard: a guard pins what the fix must not break. The ids A2 … F2
come from the review of 4cca0c5; those tests failed on 46dd0c5.
"""

from __future__ import annotations

import binascii
import io
import os
import subprocess
import zipfile
from pathlib import Path

import pikepdf
import pymupdf
import pytest
from PIL import Image, ImageCms

from app.api_errors import ApiError
from app.services import pdf_tools
from tests._env import can_ocr, pdfa_resources_present
from tests.test_audit_fixes import (
    _NO_GS,
    _NO_OCR,
    _all_text,
    _error,
    _no_tool,
    _ocr_calls,
    _photo_scan,
    _post,
    _scan,
)

# --- OCR -------------------------------------------------------------------


def test_ocr_takes_a_phone_photo_converted_to_pdf(client, monkeypatch):
    """P1-1: a 12 MP phone photo through Converter para PDF becomes a 42 x 56 in page at
    72 dpi. OCRmyPDF renders it at 72 dpi (24.4 weighted Mpx, inside the budget), but a
    check priced it at 300 dpi (212 Mpx) and answered 422 «o máximo é A2»."""
    calls = _ocr_calls(monkeypatch)
    jpeg = io.BytesIO()
    Image.new("RGB", (3024, 4032), (240, 235, 225)).save(jpeg, "JPEG", dpi=(72, 72))
    converted = _post(client, "convert", jpeg.getvalue(), name="foto.jpg", mime="image/jpeg")
    assert converted.status_code == 200
    _post(client, "ocr", converted.content, {"language": "portuguese"})
    assert calls


def test_ocr_still_refuses_a_huge_page_it_would_render_at_400_dpi(client, monkeypatch):
    """P1-1 guard: the 1 KB 5000 x 5000 pt page the A2 check was written for is refused
    by the pixel budget on its own."""
    monkeypatch.setattr(pdf_tools, "_run_command", _no_tool)
    doc = pymupdf.open()
    doc.new_page(width=5000, height=5000)
    response = _post(client, "ocr", doc.tobytes(), {"language": "portuguese"})
    assert response.status_code == 422
    assert _error(response)["code"] == "page_too_large"


@pytest.mark.skipif(not can_ocr("por"), reason=_NO_OCR)
def test_ocr_of_our_own_output_does_not_double_the_text(client):
    """P1-2: --redo-ocr strips invisible text from the page's content stream only; OCRmyPDF
    keeps its layer in a Form XObject, so a second OCR added a second copy of every word."""
    pdf = _scan(["Relatório anual da associação cultural", "Contas aprovadas em assembleia geral"])
    once = _post(client, "ocr", pdf, {"language": "portuguese"})
    twice = _post(client, "ocr", once.content, {"language": "portuguese"})
    assert twice.status_code == 200
    assert _all_text(once.content).lower().count("assembleia") == 1
    assert _all_text(twice.content).lower().count("assembleia") == 1


def _signed_scan() -> bytes:
    """A 300 dpi A4 scan signed with Assinar PDF: the pad's PNG (452 x 188 CSS px at
    devicePixelRatio 2) placed at 20 % of the page width, ~547 dpi."""
    doc = pymupdf.open()
    jpeg = io.BytesIO()
    Image.new("L", (2480, 3508), 235).save(jpeg, "JPEG")
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=jpeg.getvalue())
    sig = Image.new("RGBA", (904, 376), (17, 17, 17, 0))
    sig.paste((17, 17, 17, 255), (100, 180, 800, 190))
    png = io.BytesIO()
    sig.save(png, "PNG")
    page.insert_image(pymupdf.Rect(416, 90, 535, 139.5), stream=png.getvalue())
    return doc.tobytes()


def test_ocr_takes_a_scan_signed_at_high_resolution(client, monkeypatch):
    """P1-4: the estimator took the signature's 547 dpi for the whole page (57.8 Mpx, over
    one worker's 37.5); OCRmyPDF renders it at the area-weighted 302 dpi (17.6 Mpx)."""
    calls = _ocr_calls(monkeypatch)
    _post(client, "ocr", _signed_scan(), {"language": "portuguese"})
    assert calls


@pytest.mark.skipif(not can_ocr("eng"), reason=_NO_OCR)
def test_ocr_megapixels_match_ocrmypdf_on_a_signed_scan(tmp_path):
    """P1-4: the same page against the PNG OCRmyPDF itself writes."""
    source = tmp_path / "in.pdf"
    source.write_bytes(_signed_scan())
    work = tmp_path / "work"
    work.mkdir()
    result = subprocess.run(
        ["ocrmypdf", "-k", "--redo-ocr", "--output-type", "pdf", "-l", "eng", source, work / "o"],
        capture_output=True,
        text=True,
        env={**os.environ, "TMPDIR": str(work)},
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-500:]
    (raster,) = work.glob("*/000001_rasterize.png")
    with Image.open(raster) as im:
        rendered = im.width * im.height / 1e6 * (1 if im.mode in ("L", "1", "P") else 2)
    predicted = pdf_tools._ocr_megapixels(pymupdf.open(source)[0])
    assert predicted == pytest.approx(rendered, rel=0.02)


def _bw_scan(pages: int, dpi: int) -> bytes:
    """An MFP's black-and-white A4 scan: 1-bit CCITT G4 at `dpi`."""
    import img2pdf

    tif = io.BytesIO()
    page = Image.new("1", (round(8.27 * dpi), round(11.69 * dpi)), 1)
    page.save(tif, "TIFF", compression="group4", dpi=(dpi, dpi))
    one = pymupdf.open(stream=img2pdf.convert(tif.getvalue()), filetype="pdf")
    doc = pymupdf.open()
    for _ in range(pages):
        doc.insert_pdf(one)
    return doc.tobytes()


def test_ocr_budget_takes_six_bw_pages_at_600_dpi(client, monkeypatch):
    """P1-5: a 1-bit pixel was priced like a grey one; 6 B/W pages at 600 dpi (refused)
    ran in 3.3 s, less than the 8 colour scans the budget accepts (4.0 s)."""
    calls = _ocr_calls(monkeypatch)
    _post(client, "ocr", _bw_scan(6, 600), {"language": "portuguese"})
    assert calls


def test_ocr_does_not_price_an_inline_image_page_as_black_and_white():
    """F1: get_images does not list inline images and all([]) is True, so a page whose
    only image is an inline colour one was priced as a 1-bit scan, at half a grey page."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((10, 10), " ")  # a content stream to overwrite
    raw = bytes([120, 30, 200]) * (240 * 340)
    doc.update_stream(
        page.get_contents()[0],
        b"q 595 0 0 842 0 0 cm BI /W 240 /H 340 /CS /RGB /BPC 8 /F /AHx ID "
        + binascii.hexlify(raw)
        + b"> EI Q",
    )
    reread = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    grey = 595 * 842 / 72**2 * (340 / (842 / 72)) ** 2 / 1e6
    assert pdf_tools._ocr_megapixels(reread[0]) >= grey * 0.99


_OCR_LAYER = "OCR-Kt9epu2dhSU18DPtXGjqVg"  # ocrmypdf's name: "OCR-" + Name.random()


def _with_xobject(pdf: bytes, name: str, stream: bytes, **form) -> bytes:
    """Every page draws a Form XObject called `name` last, as OCRmyPDF draws its layer."""
    Name = pikepdf.Name
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        xobject = doc.make_stream(
            stream, Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, 595, 842], **form
        )
        for page in doc.pages:
            page.add_resource(xobject, Name.XObject, Name("/" + name))
            page.contents_add(doc.make_stream(f"q /{name} Do Q".encode()))
        buf = io.BytesIO()
        doc.save(buf)
    return buf.getvalue()


def test_ocr_budgets_our_own_output_without_its_old_layer(client, monkeypatch):
    """N1: the budget priced the old OCR layer (its invisible text renders at 400 dpi),
    stripped only afterwards: 8 colour A4 scans OCRed once were refused «até 4 páginas»."""
    calls = _ocr_calls(monkeypatch)
    jpeg = io.BytesIO()
    Image.new("RGB", (2480, 3508), (240, 235, 225)).save(jpeg, "JPEG")
    doc = pymupdf.open()
    for _ in range(8):
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=jpeg.getvalue())
    helvetica = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                   BaseFont=pikepdf.Name.Helvetica)
    layer = b"BT /F1 12 Tf 3 Tr 72 742 Td (assembleia) Tj ET"  # invisible text
    pdf = _with_xobject(doc.tobytes(), _OCR_LAYER, layer,
                        Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica)))
    _post(client, "ocr", pdf, {"language": "portuguese"})
    assert calls


def test_ocr_keeps_a_user_xobject_whose_name_starts_with_ocr(client, monkeypatch):
    """F2: the P1-2 strip took any «/OCR-… Do» for OCRmyPDF's layer, and a logo the user
    named /OCR-Logo disappeared from the page sent to OCR."""
    sent = []

    def capture(command, **_kwargs):
        sent.append(Path(command[-2]).read_bytes())
        raise ApiError(503, "tool_unavailable", "stub")

    monkeypatch.setattr(pdf_tools, "_run_command", capture)
    logo = b"1 0 0 RG 5 w 10 10 180 80 re S"  # a red box, drawn on the page
    _post(client, "ocr", _with_xobject(_scan(["Relatório anual"]), "OCR-Logo", logo),
          {"language": "portuguese"})
    with pymupdf.open(stream=sent[0], filetype="pdf") as ocr_input:
        content = b"".join(ocr_input.xref_stream(x) for x in ocr_input[0].get_contents())
    assert b"/OCR-Logo Do" in content


# --- Redact ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "912-345-678",
        "21 234 56 78",
        "93 123 45 67",
        "912 34 56 78",
        "21-234-5678",
        "00 351 912 345 678",
        "(11) 9 1234-5678",
        "11 9 1234-5678",
        "91234-5678",
        "(11) 91234 5678",
        "11 912345678",
        "0800 123 4567",
        "+55 11 9 1234-5678",
    ],
)
def test_phone_pattern_matches_pt_and_br_variants(text):
    """P1-3: 7bf5770's catch-all redacted these; b0687b4's shapes left them in the file."""
    import regex

    assert regex.fullmatch(pdf_tools.PHONE_PATTERN, text)


def test_phone_pattern_matches_after_an_abbreviation():
    """P1-3: «Tel.912345678» — the «.» after a word may start a number."""
    import regex

    assert regex.search(pdf_tools.PHONE_PATTERN, "Tel.912345678").group() == "912345678"


@pytest.mark.parametrize(
    "text",
    [
        "250.000.000,00",
        "Total 212 345,67",
        "2024-0001",
        "2023-2024",
        "CEP 01310-100",
        "FT 2024/123",
        "ISBN 978-972-0-04585-4",
        "21 23 45 67 89",
    ],
)
def test_phone_pattern_still_ignores_figures(text):
    """P1-3 guard: the wider shapes take no amount, id, date range or table row."""
    import regex

    assert regex.search(pdf_tools.PHONE_PATTERN, text) is None


def test_redact_applies_marks_left_pending_by_another_editor(client):
    """P1-6: 7bf5770 applied a pending /Redact mark on a page it redacted; b0687b4 baked
    it into a red outline over text that stays readable, on every page."""
    doc = pymupdf.open()
    one = doc.new_page()
    one.insert_text((72, 100), "Nome: Maria Silva", fontsize=12)
    one.insert_text((72, 130), "Email: maria.silva@exemplo.pt", fontsize=12)
    one.add_redact_annot(one.search_for("Maria Silva")[0])
    two = doc.new_page()
    two.insert_text((72, 100), "Morada: Rua das Flores 12", fontsize=12)
    two.add_redact_annot(two.search_for("Rua das Flores 12")[0])
    out = _post(client, "redact", doc.tobytes(), {"strategy": "email"})
    assert out.status_code == 200
    text = _all_text(out.content)
    assert "Maria Silva" not in text
    assert "Rua das Flores" not in text


def test_redact_keeps_a_jpeg_scan_a_jpeg_under_a_pending_mark(client):
    """P1-6 guard: a pending mark blanks the scan's pixels too, and the page must come
    back a JPEG, not stored lossless (ENGINE R1: outputs up to 8x the input)."""
    doc = pymupdf.open(stream=_photo_scan("Morada: Rua das Flores 12"), filetype="pdf")
    doc[0].add_redact_annot(doc[0].search_for("Rua das Flores 12")[0])
    doc.new_page().insert_text((72, 100), "Contacto: ana.costa@exemplo.pt", fontsize=12)
    pdf = doc.tobytes()
    out = _post(client, "redact", pdf, {"strategy": "email"})
    assert out.status_code == 200
    with pymupdf.open(stream=out.content, filetype="pdf") as result:
        (image,) = result[0].get_images(full=True)
        assert image[8] == "DCTDecode"
    assert len(out.content) < 2 * len(pdf)


@pytest.mark.parametrize("endpoint", ["redact/preview", "redact"])
def test_a_pending_mark_does_not_decode_an_image_over_the_budget(client, monkeypatch, endpoint):
    """A1: 4cca0c5 applied a pending mark, decoding the image under it, before any
    budget check. A 32 KB PDF peaked at 605 MiB, and the preview costs no free use."""
    monkeypatch.setattr(pdf_tools, "MAX_IMAGE_PIXELS", 100 * 100)
    applied = []
    real_apply = pymupdf.Page.apply_redactions
    monkeypatch.setattr(
        pymupdf.Page, "apply_redactions", lambda *a, **k: applied.append(1) or real_apply(*a, **k)
    )
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Contacto: joao@exemplo.pt", fontsize=12)
    image = pymupdf.Pixmap(pymupdf.csGRAY, (0, 0, 200, 200))
    page.insert_image(pymupdf.Rect(100, 300, 300, 500), pixmap=image)
    page.add_redact_annot(pymupdf.Rect(150, 350, 250, 450))
    response = _post(client, endpoint, doc.tobytes(), {"strategy": "email"})
    assert response.status_code == 422
    assert _error(response)["code"] == "image_too_large"
    assert not applied


def test_redact_does_not_show_a_print_only_annotation(client):
    """P1-8: bake() drew a NoView (print-only) stamp into the page, on screen and in the
    text."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Contacto: joao@exemplo.pt", fontsize=12)
    rect = pymupdf.Rect(300, 300, 500, 360)
    stamp = page.add_freetext_annot(rect, "COPIA", fontsize=36)
    stamp.set_flags(pymupdf.PDF_ANNOT_IS_PRINT | pymupdf.PDF_ANNOT_IS_NO_VIEW)
    stamp.update()
    out = _post(client, "redact", doc.tobytes(), {"strategy": "email"})
    with pymupdf.open(stream=out.content, filetype="pdf") as result:
        pix = result[0].get_pixmap(dpi=72, clip=rect, colorspace=pymupdf.csGRAY)
        assert min(pix.samples) > 128  # nothing drawn where the stamp was
        assert "COPIA" not in result[0].get_text()


def test_redact_does_not_show_a_print_only_form_field(client):
    """A8: P1-8 dropped NoView annotations through page.annots(), which skips form
    fields: a print-only field was still baked into the page, on screen and in the text."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Contacto: joao@exemplo.pt", fontsize=12)
    field = pymupdf.Widget()
    field.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    field.field_name = "carimbo"
    field.rect = pymupdf.Rect(300, 300, 500, 360)
    field.field_value = "COPIA"
    field.text_fontsize = 24
    annot = page.add_widget(field)
    print_only = pymupdf.PDF_ANNOT_IS_PRINT | pymupdf.PDF_ANNOT_IS_NO_VIEW
    doc.xref_set_key(annot.xref, "F", str(print_only))
    out = _post(client, "redact", doc.tobytes(), {"strategy": "email"})
    with pymupdf.open(stream=out.content, filetype="pdf") as result:
        pix = result[0].get_pixmap(dpi=72, clip=field.rect, colorspace=pymupdf.csGRAY)
        assert min(pix.samples) > 128
        assert "COPIA" not in result[0].get_text()


@pytest.mark.parametrize("endpoint", ["redact/preview", "redact"])
def test_a_pending_mark_with_overlay_text_in_an_unknown_font_is_applied(client, endpoint):
    """A2: a pending mark whose /DA names a font outside the 14 base fonts (/ArialMT,
    as other editors write it) answered 500: PyMuPDF cannot write its overlay text."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Nome: Maria Silva", fontsize=12)
    page.insert_text((72, 130), "Contacto: maria@exemplo.pt", fontsize=12)
    mark = page.add_redact_annot(page.search_for("Maria Silva")[0], text="REDACTED", fill=(0, 0, 0))
    doc.xref_set_key(mark.xref, "DA", "(/ArialMT 10 Tf 1 0 0 rg)")
    response = _post(client, endpoint, doc.tobytes(), {"strategy": "email"})
    assert response.status_code == 200, response.json()
    if endpoint == "redact":
        assert "Maria Silva" not in _all_text(response.content)


def test_a_pending_mark_over_two_lines_blacks_out_only_what_it_marks(client):
    """A3: a mark with two QuadPoints (the end of one line, the start of the next) was
    painted over its whole /Rect: unmarked text went black and stayed in the file."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Nome: Maria Silva Santos Pereira", fontsize=12)
    page.insert_text((72, 120), "Morada: Rua das Flores 12, Lisboa", fontsize=12)
    page.insert_text((72, 140), "Contacto: maria@exemplo.pt", fontsize=12)
    marked = page.search_for("Santos Pereira")[0], page.search_for("Morada:")[0]
    unmarked = page.search_for("Nome: Maria")[0], page.search_for("Rua das Flores 12")[0]
    mark = page.add_redact_annot(marked[0] | marked[1], fill=(0, 0, 0))
    h = page.rect.height  # QuadPoints are in PDF space, y up
    quads = [
        v for r in marked for v in (r.x0, h - r.y0, r.x1, h - r.y0, r.x0, h - r.y1, r.x1, h - r.y1)
    ]
    doc.xref_set_key(mark.xref, "QuadPoints", f"[{' '.join(f'{v:.2f}' for v in quads)}]")
    response = _post(client, "redact", doc.tobytes(), {"strategy": "email"})
    assert response.status_code == 200
    text = _all_text(response.content)
    assert "Santos" not in text
    assert "Morada" not in text
    with pymupdf.open(stream=response.content, filetype="pdf") as result:
        for rect in unmarked:
            pix = result[0].get_pixmap(dpi=72, clip=rect, colorspace=pymupdf.csGRAY)
            assert sum(v < 60 for v in pix.samples) / len(pix.samples) <= 0.1


# --- Damaged input and page counts -----------------------------------------


def _text_doc(pages: int):
    doc = pymupdf.open()
    for p in range(pages):
        page = doc.new_page(width=595, height=842)
        for i in range(20):
            page.insert_text((60, 80 + 20 * i), f"p{p + 1} linha {i} relatorio anual", fontsize=10)
    return doc


def _texts(pdf: bytes) -> list[str]:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return [" ".join(page.get_text().split()) for page in doc]


def test_an_object_stream_pdf_missing_only_its_xref_stream_is_processed(client):
    """P2-1: MuPDF reads all 4 pages; qpdf cannot open the file (no trailer), and its
    None was taken for a different page count: 422 damaged_pdf on every tool."""
    full = _text_doc(4).tobytes(garbage=3, deflate=True, use_objstms=True)
    cut = full[:-100]
    for endpoint in ("compress", "flatten"):
        response = _post(client, endpoint, cut)
        assert response.status_code == 200, (endpoint, response.json())
        assert _texts(response.content) == _texts(full)


@pytest.mark.parametrize("endpoint", ["compress", "flatten"])
def test_an_object_stream_pdf_cut_inside_a_page_content_is_refused(client, endpoint):
    """A5: qpdf cannot open it and every page still has a content stream, so P2-1 let
    it through: 200, the last page 19 of its 20 lines (b0687b4: 422 damaged_pdf)."""
    with pikepdf.open(io.BytesIO(_text_doc(4).tobytes(garbage=3, deflate=True))) as pdf:
        buf = io.BytesIO()
        pdf.save(buf, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    full = buf.getvalue()
    # 40 bytes before the end of the last content stream, the object before the xref stream
    cut = full[: full.rfind(b"endstream", 0, full.rfind(b"/Type /XRef")) - 40]
    response = _post(client, endpoint, cut)
    assert response.status_code == 422
    assert _error(response)["code"] == "damaged_pdf"


@pytest.mark.parametrize("endpoint", ["compress", "flatten", "protect"])
def test_a_linearized_pdf_cut_inside_its_last_page_content_is_refused(client, endpoint):
    """qpdf still counts the 8 pages of a linearized file cut inside the last page's
    content; the counts matched, the content was never read, and page 8 came back
    with lines missing behind a 200."""
    with pikepdf.open(io.BytesIO(_text_doc(8).tobytes(garbage=3, deflate=True))) as pdf:
        buf = io.BytesIO()
        pdf.save(buf, object_stream_mode=pikepdf.ObjectStreamMode.generate, linearize=True)
    full = buf.getvalue()
    cut = full[: full.rfind(b"endstream", 0, full.rfind(b"/Type /XRef")) - 40]
    options = {"userPassword": "x"} if endpoint == "protect" else None
    response = _post(client, endpoint, cut, options)
    assert response.status_code == 422
    assert _error(response)["code"] == "damaged_pdf"


def test_an_object_stream_pdf_that_lost_page_content_is_still_refused(client):
    """P2-1 guard: qpdf cannot open it either, but a page lost its content stream —
    ENGINE-14 still refuses it rather than return a page with its content missing."""
    with pikepdf.open(io.BytesIO(_text_doc(20).tobytes(garbage=3, deflate=True))) as pdf:
        buf = io.BytesIO()
        pdf.save(buf, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    response = _post(client, "compress", buf.getvalue()[:-1000])
    assert response.status_code == 422
    assert _error(response)["code"] == "damaged_pdf"


def _count_mismatch(count: int) -> bytes:
    """Four real pages under a page tree whose /Count says `count`."""
    raw = _text_doc(4).tobytes(garbage=0, deflate=False, use_objstms=False)
    assert raw.count(b"/Count 4") == 1
    return raw.replace(b"/Count 4", f"/Count {count}".encode())


_MUPDF_TOOLS = [
    ("redact/preview", {"strategy": "email"}),
    ("redact", {"strategy": "email"}),
    ("compress", None),
    ("flatten", None),
    ("pdf-to-image", {"format": "png", "pages": "all"}),
    ("pdf-to-word", None),
]


@pytest.mark.parametrize(("endpoint", "options"), _MUPDF_TOOLS)
def test_a_page_the_tree_count_leaves_out_is_refused(client, endpoint, options):
    """/Count 3 over 4 pages: MuPDF saw 3, viewers show 4, and Censurar left page 4's
    email readable in a file whose preview never listed it."""
    response = _post(client, endpoint, _count_mismatch(3), options)
    assert response.status_code == 422
    assert _error(response)["code"] == "damaged_pdf"


def test_a_page_tree_too_big_to_walk_is_counted_by_qpdf(client, monkeypatch):
    """Past MAX_PAGE_TREE_WALK objects qpdf counts the /Kids, in its own process."""
    monkeypatch.setattr(pdf_tools, "MAX_PAGE_TREE_WALK", 2)
    assert _post(client, "flatten", _count_mismatch(4)).status_code == 200
    response = _post(client, "flatten", _count_mismatch(3))
    assert response.status_code == 422
    assert _error(response)["code"] == "damaged_pdf"


def test_a_tree_count_above_the_objects_is_refused_not_a_500(client):
    """/Count 1 000 000 over 4 pages: MuPDF refuses a /Count above its object count,
    and the RuntimeError from page_count escaped as a 500."""
    response = _post(client, "compress", _count_mismatch(1_000_000))
    assert response.status_code == 422
    assert _error(response)["code"] == "damaged_pdf"


def test_pdf_to_image_zip_survives_a_page_tree_count_above_its_pages(client):
    """P2-2: /Count 6 over 4 pages — the image budget indexed page 5 and answered 500."""
    response = _post(client, "pdf-to-image", _count_mismatch(6), {"format": "jpeg", "pages": "all"})
    assert response.status_code == 200, response.json()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == 4


@pytest.mark.skipif(not pdfa_resources_present(), reason=_NO_GS)
@pytest.mark.parametrize("count", [6, 3])
def test_pdfa_converts_a_page_tree_whose_count_disagrees(client, count):
    """P2-2: PDF/A compared Ghostscript's 4 pages with the /Count and refused (422)."""
    response = _post(client, "pdfa", _count_mismatch(count), {"conformance": "pdfa-2b"})
    assert response.status_code == 200, response.json()
    with pikepdf.open(io.BytesIO(response.content)) as pdf:
        assert len(pdf.pages) == 4


def test_repair_does_not_report_lost_pages_for_a_wrong_count(client):
    """P2-2: Repair told the user «Recuperámos 4 de 6 páginas» for an intact file."""
    response = _post(client, "pdf-repair", _count_mismatch(6))
    assert response.status_code == 200
    assert response.headers["X-Repair-Status"] == "already-healthy"
    assert response.headers["X-Repair-Pages"] == "4/4"


def _raw_pdf(objects: dict[int, bytes]) -> bytes:
    """A PDF written object by object with a classic xref; object 1 is the catalog."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (num, objects[num])
    size, start = max(objects) + 1, len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for num in range(1, size):
        out += b"%010d 00000 n \n" % offsets[num] if num in offsets else b"0000000000 65535 f \n"
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, start)
    return bytes(out)


_HELVETICA = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def test_repair_reports_the_pages_lost_with_a_page_tree_node(client):
    """A4: one of three /Pages nodes (10 of 30 pages) is gone. MuPDF's page walk raises
    on it, the count fell to 0 instead of the /Count, and Repair sold 20 of 20 pages as
    «already-healthy» (b0687b4: partial, 20/30)."""
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 3: _HELVETICA}
    nodes, num = [], 4
    for n in range(3):
        node, kids = num, []
        num += 1
        for p in range(10):
            text = b"BT /F1 12 Tf 72 700 Td (no %d pagina %d) Tj ET" % (n, p)
            objects[num] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text)
            objects[num + 1] = (
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Contents %d 0 R"
                b" /Resources << /Font << /F1 3 0 R >> >> >>" % (node, num)
            )
            kids.append(b"%d 0 R" % (num + 1))
            num += 2
        objects[node] = b"<< /Type /Pages /Parent 2 0 R /Kids [%s] /Count 10 >>" % b" ".join(kids)
        nodes.append(node)
    root_kids = b" ".join(b"%d 0 R" % n for n in nodes)
    objects[2] = b"<< /Type /Pages /Kids [%s] /Count 30 >>" % root_kids
    del objects[nodes[1]]
    response = _post(client, "pdf-repair", _raw_pdf(objects))
    assert response.status_code == 200
    assert response.headers["X-Repair-Status"] == "partial"
    assert response.headers["X-Repair-Pages"] == "20/30"


def _deep_chain(depth: int) -> bytes:
    """One page under a chain of `depth` /Pages nodes, one kid each."""
    text = b"BT /F1 12 Tf 72 700 Td (Contacto) Tj ET"
    objects = {
        1: b"<< /Type /Catalog /Pages 5 0 R >>",
        2: _HELVETICA,
        3: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text),
        4: b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Contents 3 0 R"
        b" /Resources << /Font << /F1 2 0 R >> >> >>" % (4 + depth),
    }
    for k in range(5, 5 + depth):  # a chain of /Pages nodes, one kid each
        parent = b"/Parent %d 0 R " % (k - 1) if k > 5 else b""
        kid = k + 1 if k < 4 + depth else 4
        objects[k] = b"<< /Type /Pages %s/Kids [%d 0 R] /Count 1 >>" % (parent, kid)
    return _raw_pdf(objects)


def _four_pages(kids: bytes, count: int, extra: dict[int, bytes] | None = None) -> bytes:
    """Pages 21–24 under the /Pages node 2, whose /Kids and /Count are given as is."""
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, count),
        3: _HELVETICA,
        **(extra or {}),
    }
    for n in range(1, 5):
        text = b"BT /F1 12 Tf 72 700 Td (Pagina %d) Tj ET" % n
        objects[10 + n] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text)
        objects[20 + n] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R"
            b" /Resources << /Font << /F1 3 0 R >> >> >>" % (10 + n)
        )
    return _raw_pdf(objects)


_UNOPENABLE_KIDS = {
    "null": _four_pages(b"21 0 R 22 0 R null 23 0 R 24 0 R", 4),
    "missing object": _four_pages(b"21 0 R 22 0 R 99 0 R 23 0 R 24 0 R", 4),
    "cycle": _four_pages(
        b"21 0 R 22 0 R 5 0 R",
        4,
        {5: b"<< /Type /Pages /Parent 2 0 R /Kids [2 0 R 23 0 R] /Count 2 >>"},
    ),
}
_PDFA = ("pdfa", {"conformance": "pdfa-2b"})


@pytest.mark.parametrize("kids", _UNOPENABLE_KIDS)
@pytest.mark.parametrize(("endpoint", "options"), [*_MUPDF_TOOLS, _PDFA])
def test_a_page_tree_entry_mupdf_cannot_open_is_refused(client, kids, endpoint, options):
    """A /Kids entry that is null, a missing object or a node reached twice (a cycle):
    MuPDF counts it and fails on it, a 500 on most tools (b0687b4..defc964), while
    Flatten returned pages shifted out of place behind a 200."""
    if endpoint == "pdfa" and not pdfa_resources_present():
        pytest.skip(_NO_GS)
    response = _post(client, endpoint, _UNOPENABLE_KIDS[kids], options)
    assert response.status_code == 422, response.content[:200]
    assert _error(response)["code"] == "damaged_pdf"


_MISREAD_TREES = {"count 3 over 4": _count_mismatch(3), **_UNOPENABLE_KIDS}


@pytest.mark.parametrize("tree", _MISREAD_TREES)
def test_repair_fixes_a_page_tree_the_tools_refuse(client, tree):
    """Every tool refuses these with «Use primeiro a ferramenta Reparar PDF», and Repair
    called them already-healthy and returned them unchanged: a dead end."""
    response = _post(client, "pdf-repair", _MISREAD_TREES[tree])
    assert response.status_code == 200, response.content[:200]
    assert response.headers["X-Repair-Status"] != "already-healthy"
    recovered = int(response.headers["X-Repair-Pages"].split("/")[0])
    assert len(_texts(response.content)) == recovered  # 4 of 4; the cycle, 2 of 4 by gs
    pdf_tools._open_pdf(response.content).close()  # the tools now accept it


def test_the_qpdf_page_count_survives_a_page_tree_40000_levels_deep():
    """A6: PDF/A counted pages with pikepdf inside the API process; qpdf recurses down
    the page tree, and 40 000 levels overflowed its stack and killed the worker (SIGBUS)."""
    # Returning at all is the point: qpdf may give up (None) or count the one page.
    assert pdf_tools._qpdf_page_count(_deep_chain(40_000)) in (None, 1)


@pytest.mark.parametrize("endpoint", ["protect", "pdf-unlock"])
def test_protect_and_unlock_refuse_a_page_tree_40000_levels_deep(client, endpoint):
    """Both open the upload with pikepdf inside the API process: 20 000 levels
    overflowed qpdf's recursive page walk and killed the worker (SIGSEGV)."""
    pdf, options = _deep_chain(40_000), {"userPassword": "x"}
    if endpoint == "pdf-unlock":  # owner-only encryption, as Unlock expects
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        pdf, options = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o"), {}
    response = _post(client, endpoint, pdf, options)
    assert response.status_code == 422
    assert _error(response)["code"] == "damaged_pdf"


# --- Convert ---------------------------------------------------------------


def test_convert_an_icc_tiff_without_resolution_keeps_a_normal_page(client):
    """P2-4: Pillow reads a TIFF without resolution tags as 1 dpi; re-saved without the ICC
    profile, img2pdf honoured it and made a page 1 200 inches wide."""
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    buf = io.BytesIO()
    Image.new("RGB", (1200, 900), "red").save(buf, format="TIFF", icc_profile=icc)
    response = _post(client, "convert", buf.getvalue(), name="x.tif", mime="image/tiff")
    assert response.status_code == 200
    with pymupdf.open(stream=response.content, filetype="pdf") as doc:
        assert round(doc[0].rect.width) == 900  # 1 200 px at img2pdf's 96 dpi, as without ICC


@pytest.mark.parametrize(
    ("fmt", "name", "mime"), [("WEBP", "foto.jpg", "image/jpeg"), ("GIF", "foto.png", "image/png")]
)
def test_convert_accepts_a_webp_or_gif_saved_under_an_image_name(client, fmt, name, mime):
    """P2-5: a WebP saved as .jpg opens in every viewer; b0687b4 called it «não é um JPG
    válido» (422)."""
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), "blue").save(buf, format=fmt)
    response = _post(client, "convert", buf.getvalue(), name=name, mime=mime)
    assert response.status_code == 200, response.json()
