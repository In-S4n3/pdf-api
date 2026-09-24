"""Regressions found by the audit's final review (2026-09-24), ids P1-1 … P2-5.

Each test failed on b0687b4, the audit's last deploy, unless its docstring
calls it a guard: a guard pins what the fix must not break.
"""

from __future__ import annotations

import io
import os
import subprocess
import zipfile

import pikepdf
import pymupdf
import pytest
from PIL import Image, ImageCms

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
