"""Regressions from the 2026-09-23 audit (APICODE / ENGINE / CONTRACT / PAIDUI).

Each test failed on the code before the fix. They go through the HTTP client
where they can, so the contract is what is pinned, not a helper's shape.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import suppress
from pathlib import Path

import pikepdf
import pymupdf
import pytest
from PIL import Image, ImageCms

from app import router_v2
from app.api_errors import ApiError
from app.services import pdf_tools
from tests._env import can_ocr, has_soffice, pdfa_resources_present

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import gen_excel_fixtures as g  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
DEJAVU = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
CMYK_ICC = Path("/usr/share/color/icc/ghostscript/default_cmyk.icc")
_NO_OCR = "requires the OCR toolchain (Docker-only)"
_NO_GS = "requires Ghostscript PDF/A resources (Docker-only)"


def _post(client, endpoint, content, options=None, *, name="in.pdf", mime="application/pdf"):
    return client.post(
        f"/v2/{endpoint}",
        files={"file": (name, io.BytesIO(content), mime)},
        data={"options": json.dumps(options or {})},
    )


def _error(response) -> dict:
    return response.json()["error"]


def _all_text(pdf: bytes) -> str:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return " ".join(" ".join(page.get_text().split()) for page in doc)


def _objects_holding(pdf: bytes, needle: str) -> list:
    """Every object (strings and decoded streams) that still carries `needle`."""
    encodings = (needle.encode("utf-8"), needle.encode("utf-16-be"))
    found = []
    with pikepdf.open(io.BytesIO(pdf)) as doc:
        for obj in doc.objects:
            blobs = [repr(obj).encode("utf-8", "replace")]
            if isinstance(obj, pikepdf.Stream):
                with suppress(pikepdf.PdfError):
                    blobs.append(obj.read_bytes())
            if any(enc in blob for enc in encodings for blob in blobs):
                found.append(obj.objgen)
    return found


def _text_pdf(lines, *, pages=1, size=(595, 842)) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=size[0], height=size[1])
        for i, line in enumerate(lines):
            page.insert_text((72, 90 + 24 * i), line, fontsize=14)
    return doc.tobytes()


def _scan(lines, *, ocr_layer=False, footer=None, pages=1) -> bytes:
    """A 'scanned' page: the text only as pixels, optionally with an invisible
    OCR layer at the same place (what our own OCR tool produces)."""
    with pymupdf.open(stream=_text_pdf(lines), filetype="pdf") as src:
        pix = src[0].get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, pixmap=pix)
        if ocr_layer:
            for i, line in enumerate(lines):
                page.insert_text((72, 90 + 24 * i), line, fontsize=14, render_mode=3)
        if footer:
            page.insert_text((72, 830), footer, fontsize=6)
    return doc.tobytes()


# --- Redact ----------------------------------------------------------------


def test_redact_blanks_the_scan_pixels_under_the_box(client):
    """APICODE-01 / ENGINE-01 / CONTRACT-01 (P0): on an OCR'd scan the black
    box hid only the text layer; the email stayed readable in the page image."""
    pdf = _scan(["Contacto: ana.costa@exemplo.pt"], ocr_layer=True)
    preview = _post(client, "redact/preview", pdf, {"strategy": "email"}).json()
    box = pymupdf.Rect(preview["matches"][0]["bbox"])

    out = _post(client, "redact", pdf, {"strategy": "email"})
    assert out.status_code == 200
    with pymupdf.open(stream=out.content, filetype="pdf") as doc:
        page = doc[0]
        info = page.get_image_info(xrefs=True)[0]
        pix = pymupdf.Pixmap(doc, info["xref"])
        scale = pix.width / pymupdf.Rect(info["bbox"]).width
        dark = sum(
            pix.pixel(int(x * scale), int(y * scale))[0] < 128
            for x in range(int(box.x0), int(box.x1))
            for y in range(int(box.y0), int(box.y1))
        )
    assert dark == 0
    assert "ana.costa@exemplo.pt" not in _all_text(out.content)


def _photo_scan(line: str) -> bytes:
    """A colour photo of a page, stored as JPEG like a phone scan, with an
    invisible OCR layer over the text."""
    with pymupdf.open() as src:
        page = src.new_page(width=595, height=842)
        for i in range(1500):  # a busy background: lossless storage balloons
            x, y = (i * 37) % 595, (i * 53) % 842
            fill = ((i % 7) / 6, (i % 5) / 4, (i % 3) / 2)
            page.draw_circle((x, y), 4 + i % 17, color=None, fill=fill)
        page.insert_text((72, 90), line, fontsize=14)
        jpeg = page.get_pixmap(dpi=150).tobytes("jpeg", jpg_quality=75)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=jpeg)
    page.insert_text((72, 90), line, fontsize=14, render_mode=3)
    return doc.tobytes()


def test_redact_keeps_a_jpeg_scan_a_jpeg(client):
    """ENGINE R1 (P1): blanking the pixels under the box rewrote every JPEG it
    touched as a lossless image — outputs up to 8x the input, past the 32 MiB
    Cloud Run can deliver."""
    pdf = _photo_scan("Contacto: ana.costa@exemplo.pt")
    preview = _post(client, "redact/preview", pdf, {"strategy": "email"}).json()
    box = pymupdf.Rect(preview["matches"][0]["bbox"]) + (1, 1, -1, -1)  # inside, not the row above

    out = _post(client, "redact", pdf, {"strategy": "email"})
    assert out.status_code == 200
    with pymupdf.open(stream=out.content, filetype="pdf") as doc:
        page = doc[0]
        (image,) = page.get_images(full=True)
        assert image[8] == "DCTDecode"
        pix = pymupdf.Pixmap(doc, image[0])
        scale = pix.width / page.rect.width
        dark = sum(
            pix.pixel(int(x * scale), int(y * scale))[0] < 128
            for x in range(int(box.x0), int(box.x1))
            for y in range(int(box.y0), int(box.y1))
        )
    assert dark == 0  # the fix for ENGINE-01 still holds
    assert len(out.content) < 2 * len(pdf)


def test_redact_refuses_output_above_the_response_cap(client, monkeypatch):
    """ENGINE R1 (P1): the redact route had no size guard, so a 48-123 MB
    result was built and then dropped by Cloud Run."""
    monkeypatch.setattr(pdf_tools, "MAX_RESPONSE_BYTES", 100, raising=False)
    response = _post(client, "redact", _text_pdf(["ana.costa@exemplo.pt"]), {"strategy": "email"})
    assert response.status_code == 422
    assert _error(response)["code"] == "output_too_large"


def test_redact_reaches_sticky_notes_and_form_field_appearances(client):
    """APICODE-02 / ENGINE-02 / CONTRACT-09 (P0): the email survived in a note's
    /Contents and in a text field's appearance stream (copyable, searchable)."""
    email = "maria.santos@exemplo.pt"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Formulário de contacto", fontsize=12)
    page.add_text_annot((400, 100), f"Nota: ligar a {email}")
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "email"
    widget.field_value = email
    widget.rect = pymupdf.Rect(72, 200, 372, 225)
    page.add_widget(widget)
    pdf = doc.tobytes()
    assert _objects_holding(pdf, email)  # the fixture really carries it

    out = _post(client, "redact", pdf, {"strategy": "email"})
    assert out.status_code == 200
    assert _objects_holding(out.content, email) == []
    with pymupdf.open(stream=out.content, filetype="pdf") as result:
        assert not list(result[0].annots())


def test_redact_custom_text_across_a_line_break_and_a_non_breaking_space(client):
    """ENGINE-15 (P1): 'Joaquim Gonçalves' was missed when wrapped over two
    lines or joined by U+00A0 — the preview said 0 and the name stayed."""
    if not DEJAVU.exists():
        pytest.skip("needs DejaVuSans for a real U+00A0")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Cliente: Joaquim\xa0Gonçalves.",
        fontsize=12,
        fontname="dejavu",
        fontfile=str(DEJAVU),
    )
    page.insert_textbox(
        pymupdf.Rect(72, 100, 250, 200),
        "O associado pediu que Joaquim Gonçalves fosse contactado.",
        fontsize=12,
    )
    pdf = doc.tobytes()
    assert "\xa0" in doc[0].get_text() and "Joaquim\nGonçalves" in doc[0].get_text()

    options = {"strategy": "custom", "customText": "Joaquim Gonçalves"}
    preview = _post(client, "redact/preview", pdf, options).json()
    assert preview["total"] >= 4  # two words, twice
    out = _post(client, "redact", pdf, options)
    assert "Joaquim" not in _all_text(out.content)


def test_redact_removes_matches_past_the_preview_cap(client, monkeypatch):
    """CONTRACT-08 (P1): the preview lists 5 000 matches; apply redacted only
    the ids it was sent, so the rest stayed (200 of 5 200 emails)."""
    monkeypatch.setattr(router_v2, "_PREVIEW_MATCH_CAP", 2)
    monkeypatch.setattr(pdf_tools, "PREVIEW_MATCH_CAP", 2, raising=False)
    pdf = _text_pdf([f"pessoa{i}@exemplo.pt" for i in range(5)])

    preview = _post(client, "redact/preview", pdf, {"strategy": "email"}).json()
    assert preview["truncated"] is True and len(preview["matches"]) == 2
    ids = [m["id"] for m in preview["matches"]]
    out = _post(client, "redact", pdf, {"strategy": "email", "confirmed_ids": ids})
    assert "@" not in _all_text(out.content)

    # A previewed match the user deselected still survives.
    out = _post(client, "redact", pdf, {"strategy": "email", "confirmed_ids": ids[1:]})
    assert _all_text(out.content).count("@") == 1


def test_phone_strategy_takes_phones_not_table_figures(client):
    """PAIDUI-04 (P1): 456 'phones' in a 3-page contract — every table figure,
    date and amount blacked out."""
    lines = [
        "Contacto: +351 912 345 678 ou 21 234 5678.",
        "Mês   Receita   Despesa   Saldo",
        "1 000   1 037   1 074   1 111",
        "Data 2024-01-15, pago a 12/03/2024: 1.234.567,89 EUR.",
        "NIF 123456789, código postal 7000-123 Évora.",
        "IBAN PT50 0002 0123 1234 5678 9015 4.",
    ]
    preview = _post(client, "redact/preview", _text_pdf(lines), {"strategy": "phone"}).json()
    assert {m["fullMatch"] for m in preview["matches"]} == {"+351 912 345 678", "21 234 5678"}


@pytest.mark.parametrize(
    "text",
    [
        "912 345 678",
        "+351912345678",
        "(+351) 912 345 678",
        "00351 912 345 678",
        "(11) 91234-5678",
        "+55 11 91234-5678",
        "+44 20 7946 0958",
        "+33 6 12 34 56 78",
    ],
)
def test_phone_pattern_matches_real_numbers(text):
    import regex

    assert regex.fullmatch(pdf_tools.PHONE_PATTERN, text)


@pytest.mark.parametrize(
    "text",
    [
        "2024-01-15",
        "12/03/2024",
        "1.234.567",
        "123456789",
        "1 000",
        "7000-123",
        "Saldo 1 234 567,89",
        "+1 234 567",
        "3 000\n3 037",
        "PT50 0035 0123 0001 2345 6789 0",
    ],
)
def test_phone_pattern_ignores_figures(text):
    import regex

    assert regex.search(pdf_tools.PHONE_PATTERN, text) is None


# --- Compress --------------------------------------------------------------


def test_compress_keeps_icc_cmyk_images_intact(client):
    """ENGINE-03 (P0): an ICC-based CMYK photo (how InDesign exports) came back
    as grey stripes and a black band, with a 200."""
    if not CMYK_ICC.exists():
        pytest.skip("needs Ghostscript's default_cmyk.icc")
    pix = pymupdf.Pixmap(pymupdf.csCMYK, pymupdf.IRect(0, 0, 600, 400), False)
    for i, cmyk in enumerate([(255, 0, 0, 0), (0, 255, 0, 0), (0, 0, 255, 0), (40, 40, 40, 200)]):
        pix.set_rect(pymupdf.IRect(150 * i, 0, 150 * (i + 1), 400), cmyk)
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=133)  # 216 dpi: above the 150 dpi threshold
    page.insert_image(page.rect, pixmap=pix)
    xref = page.get_images()[0][0]
    icc = doc.get_new_xref()
    doc.update_object(icc, "<</N 4>>")
    doc.update_stream(icc, CMYK_ICC.read_bytes())
    doc.xref_set_key(xref, "ColorSpace", f"[/ICCBased {icc} 0 R]")
    pdf = doc.tobytes()

    out = _post(client, "compress", pdf)
    assert out.status_code == 200

    def render(data):
        with pymupdf.open(stream=data, filetype="pdf") as d:
            return d[0].get_pixmap(dpi=36, colorspace=pymupdf.csRGB).samples

    before, after = render(pdf), render(out.content)
    assert sum(abs(a - b) for a, b in zip(before, after, strict=True)) / len(before) < 3


# --- PDF/A -----------------------------------------------------------------


@pytest.mark.skipif(not pdfa_resources_present(), reason=_NO_GS)
@pytest.mark.parametrize(
    ("content", "code"),
    [
        ((FIXTURES / "encrypted.pdf").read_bytes(), "password_protected_pdf"),
        (b"", "invalid_pdf"),
        (
            b"%!PS-Adobe-3.0\n/Helvetica findfont 12 scalefont setfont\n"
            b"72 720 moveto (POSTSCRIPT RAN) show showpage\n",
            "invalid_pdf",
        ),
    ],
    ids=["password", "empty", "postscript"],
)
def test_pdfa_refuses_input_ghostscript_would_turn_into_a_blank_page(client, content, code):
    """APICODE-07 / ENGINE-04 / CONTRACT-02 (P0) and APICODE-08 (P1): gs exits 0
    with one blank page for a password or an empty file (200, and veraPDF
    passes it), and runs a %!PS upload as a program."""
    response = _post(client, "pdfa", content)
    assert response.status_code == 400
    assert _error(response)["code"] == code


@pytest.mark.skipif(not pdfa_resources_present(), reason=_NO_GS)
def test_pdfa_1b_refuses_to_turn_text_into_a_picture(client):
    """ENGINE-13 (P1): PDF/A-1b has no transparency, so gs rasterised a whole
    invoice page for its transparent logo — no text left to search or copy."""
    doc = pymupdf.open()
    page = doc.new_page()
    for i in range(10):
        line = f"Fatura n.º {i}: 1.234,56 EUR — serviço prestado"
        page.insert_text((72, 200 + 20 * i), line, fontsize=12)
    logo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 40), True)
    logo.set_rect(logo.irect, (200, 30, 30, 128))  # half-transparent
    page.insert_image(pymupdf.Rect(72, 60, 232, 140), pixmap=logo)
    pdf = doc.tobytes()

    response = _post(client, "pdfa", pdf, {"conformance": "pdfa-1b"})
    assert response.status_code == 422
    assert _error(response)["code"] == "pdfa1_transparency"
    assert _post(client, "pdfa", pdf, {"conformance": "pdfa-2b"}).status_code == 200


@pytest.mark.skipif(not pdfa_resources_present(), reason=_NO_GS)
def test_pdfa_keeps_the_title_and_the_links(client):
    """ENGINE-20 (P2): the stock template stamped «Title» over the document's
    title, and links without the Print flag were dropped."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Contacte-nos", fontsize=12)
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(72, 60, 200, 76),
            "uri": "mailto:geral@exemplo.pt",
        }
    )
    doc.set_metadata({"title": "Relatório Anual 2025"})
    out = _post(client, "pdfa", doc.tobytes())
    assert out.status_code == 200
    with pymupdf.open(stream=out.content, filetype="pdf") as result:
        assert result.metadata["title"] == "Relatório Anual 2025"
        assert [link["uri"] for link in result[0].get_links()] == ["mailto:geral@exemplo.pt"]


# --- PDF to image ----------------------------------------------------------


def test_pdf_to_image_bounds_the_pixels_of_a_huge_page(client):
    """ENGINE-05 / APICODE-10 (P0): 300 dpi of a 5000 x 5000 pt page is
    2.25 Gpx; a 1 KB upload OOM-killed the 2 GiB instance."""
    pdf = _text_pdf(["Planta"], size=(3000, 3000))  # 156 Mpx at 300 dpi
    response = _post(client, "pdf-to-image", pdf)
    assert response.status_code == 200
    image = Image.open(io.BytesIO(response.content))
    assert image.width * image.height <= pdf_tools.MAX_RENDER_PIXELS


def test_pdf_to_image_refuses_output_above_the_response_cap(client, monkeypatch):
    """CONTRACT-07 / APICODE-10 / ENGINE-10 (P1): 20 PNG pages made 104 MiB;
    Cloud Run drops a response above 32 MiB after all the work is done."""
    monkeypatch.setattr(pdf_tools, "MAX_RESPONSE_BYTES", 50_000, raising=False)
    pdf = _text_pdf([f"Linha {i} " * 8 for i in range(30)], pages=4)
    response = _post(client, "pdf-to-image", pdf, {"pages": "all", "format": "png"})
    assert response.status_code == 422
    assert _error(response)["code"] == "output_too_large"


def test_pdf_to_image_names_the_extract_tool_as_the_site_does(client, monkeypatch):
    """CONTRACT verify (P3): the messages sent users to «Extrair Páginas»; the
    site calls the tool «Extrair PDF»."""
    over_cap = _post(client, "pdf-to-image", _text_pdf(["x"], pages=21), {"pages": "all"})
    monkeypatch.setattr(pdf_tools, "MAX_RESPONSE_BYTES", 1_000, raising=False)
    jpeg_zip = {"pages": "all", "format": "jpeg"}
    too_big = _post(client, "pdf-to-image", _text_pdf(["x"], pages=2), jpeg_zip)
    for response in (over_cap, too_big):
        message = _error(response)["message"]
        assert "Extrair PDF" in message and "Extrair Páginas" not in message, message


def test_pdf_to_image_password_and_non_pdf_are_user_errors(client):
    """CONTRACT-16 / ENGINE-22 (P2): a protected PDF answered 500, a PNG named
    .pdf answered 200 with the PNG echoed back."""
    protected = _post(client, "pdf-to-image", (FIXTURES / "encrypted.pdf").read_bytes())
    assert (protected.status_code, _error(protected)["code"]) == (400, "password_protected_pdf")
    png = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(png, format="PNG")
    not_pdf = _post(client, "pdf-to-image", png.getvalue())
    assert (not_pdf.status_code, _error(not_pdf)["code"]) == (400, "invalid_pdf")


# --- Subprocesses ----------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        state = next(
            line
            for line in Path(f"/proc/{pid}/status").read_text().splitlines()
            if line.startswith("State:")
        )
    except (FileNotFoundError, StopIteration):
        return False
    return " Z " not in f" {state.split(maxsplit=1)[1]} "  # a zombie is dead


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc (Linux)")
def test_timeout_kills_the_whole_process_group_and_its_temp_files(tmp_path):
    """APICODE-03 (P0) / APICODE-04 (P1): a timeout killed only the direct child
    — soffice.bin and ocrmypdf's workers kept running — and ocrmypdf's work dir
    stayed in RAM-backed /tmp (534 MB from two requests)."""
    pid_file = tmp_path / "grandchild.pid"
    script = f'sleep 30 & echo $! > {pid_file}; touch "$TMPDIR/work-file"; wait'
    with tempfile.TemporaryDirectory() as tmpdir:
        started = time.monotonic()
        with pytest.raises(ApiError) as exc:
            pdf_tools._run_command(["sh", "-c", script], timeout=1, tmpdir=tmpdir)
        assert exc.value.status_code == 504
        assert time.monotonic() - started < 10
        assert (Path(tmpdir) / "work-file").exists()  # the tool wrote into our dir
    time.sleep(0.2)
    assert not _alive(int(pid_file.read_text()))


# --- OCR -------------------------------------------------------------------


def _no_tool(*_args, **_kwargs):
    raise AssertionError("the tool must not run for this input")


def test_ocr_refuses_more_pages_than_fit_the_time_budget(client, monkeypatch):
    """APICODE-05 / ENGINE-06 / CONTRACT-06 (P1): no page cap, so a 10-page
    scan burned 45 s and answered 504; refuse up front, with the number."""
    monkeypatch.setattr(pdf_tools, "_run_command", _no_tool)
    cap = getattr(pdf_tools, "MAX_OCR_PAGES", 8)
    pdf = _scan(["Digitalizado"], pages=cap + 2)
    response = _post(client, "ocr", pdf, {"language": "portuguese"})
    assert response.status_code == 422
    assert _error(response)["code"] == "too_many_pages"
    assert str(cap) in _error(response)["message"]
    # Dividir PDF makes exactly two parts; Extrair PDF takes the range that fits.
    assert "Extrair PDF" in _error(response)["message"]


def _ocr_calls(monkeypatch) -> list:
    """Record each ocrmypdf call instead of running it: a job that reaches it
    got past every OCR limit."""
    calls = []

    def record(command, **_kwargs):
        calls.append(command)
        raise ApiError(503, "tool_unavailable", "stub")

    monkeypatch.setattr(pdf_tools, "_run_command", record)
    return calls


def _photo_pages(count: int):
    """A4 photo pages with a caption: 30.9 Mpx each (400 dpi, colour)."""
    page = pymupdf.open(stream=_photo_scan("Legenda da fotografia"), filetype="pdf")
    doc = pymupdf.open()
    for _ in range(count):
        doc.insert_pdf(page)
    return doc


def _colour_scan_page(doc) -> None:
    """An A4 page holding a 300 dpi colour scan and no text: 17.4 Mpx."""
    jpeg = io.BytesIO()
    Image.new("RGB", (2480, 3508), (235, 230, 220)).save(jpeg, "JPEG")
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=jpeg.getvalue())


def test_ocr_budgets_the_pixels_not_only_the_pages(client, monkeypatch):
    """ENGINE R2 (P2): six photo pages with captions sat under the page cap and
    took 31 s on the 2 CPU bench (≈55 s on Cloud Run, past the 45 s kill):
    OCRmyPDF renders a page with any text at 400 dpi, in colour."""
    monkeypatch.setattr(pdf_tools, "_run_command", _no_tool)
    response = _post(client, "ocr", _photo_pages(5).tobytes(), {"language": "portuguese"})
    assert response.status_code == 422
    assert _error(response)["code"] == "too_many_pages"
    assert "até 4 páginas" in _error(response)["message"]
    assert "Extrair PDF" in _error(response)["message"]


def test_ocr_budget_still_takes_eight_ordinary_scans(client, monkeypatch):
    """The pixel budget must not undercut the page cap for the scans it was
    measured on: 8 colour A4 pages at 300 dpi, no text (28.5 s on Cloud Run
    gen2 with 4 vCPU; a 504 at 45 s on 2 vCPU)."""
    calls = _ocr_calls(monkeypatch)
    doc = pymupdf.open()
    for _ in range(8):
        _colour_scan_page(doc)
    _post(client, "ocr", doc.tobytes(), {"language": "portuguese"})
    assert calls


def test_ocr_takes_four_photo_pages_with_captions(client, monkeypatch):
    """Four A4 photos with captions fill the four workers once: 39-40 s on
    Cloud Run gen2 with 4 vCPU, a 504 on 2 vCPU. The per-worker cap must not
    refuse them."""
    calls = _ocr_calls(monkeypatch)
    _post(client, "ocr", _photo_pages(4).tobytes(), {"language": "portuguese"})
    assert calls


def test_ocr_refuses_a_page_one_worker_cannot_finish(client, monkeypatch):
    """A page never splits across workers. An A3 photo with a caption renders
    61.9 Mpx, two workers' share (≈60 s on one Cloud Run vCPU, past the 45 s
    kill); a cap of half the budget let it through."""
    monkeypatch.setattr(pdf_tools, "_run_command", _no_tool)
    doc = _photo_pages(1)
    doc[0].set_mediabox(pymupdf.Rect(0, 0, 842, 1191))  # A3
    response = _post(client, "ocr", doc.tobytes(), {"language": "portuguese"})
    assert response.status_code == 422
    assert _error(response)["code"] == "page_too_large"


def test_ocr_budgets_the_busiest_worker_not_the_total(client, monkeypatch):
    """Four photo pages fill the four workers (39-40 s on Cloud Run); a fifth
    waits for one to free up, then runs ~15 s more, past the 45 s kill —
    though the total, 141 Mpx, is under the 150 budget."""
    monkeypatch.setattr(pdf_tools, "_run_command", _no_tool)
    doc = _photo_pages(4)
    _colour_scan_page(doc)
    response = _post(client, "ocr", doc.tobytes(), {"language": "portuguese"})
    assert response.status_code == 422
    assert _error(response)["code"] == "too_many_pages"
    assert "até 4 páginas" in _error(response)["message"]


def test_ocr_runs_one_worker_per_cloud_run_vcpu():
    """More workers than vCPUs thrash (10 on 2 CPUs took ~1 GB); fewer leave
    the per-worker budget counting cores that never run. The deploy sets the
    vCPUs, so the workflow and service.yaml must match --jobs."""
    root = Path(__file__).resolve().parent.parent
    jobs = pdf_tools.OCR_FLAGS[pdf_tools.OCR_FLAGS.index("--jobs") + 1]
    deploy = (root / ".github" / "workflows" / "deploy.yml").read_text()
    service = (root / "service.yaml").read_text()
    assert re.findall(r"--cpu=(\d+)", deploy) == [jobs]
    assert re.findall(r'cpu: "(\d+)"', service) == [jobs]


def _grey_pixmap(side: int) -> pymupdf.Pixmap:
    pix = pymupdf.Pixmap(pymupdf.csGRAY, (0, 0, side, side), 0)
    pix.clear_with(230)
    return pix


def _grey_scan_page(doc, *, footer=None, drawing=False):
    """A 2 x 2 inch page holding a 200 dpi DeviceGray scan."""
    page = doc.new_page(width=144, height=144)
    xref = page.insert_image(page.rect, pixmap=_grey_pixmap(400))
    doc.xref_set_key(xref, "ColorSpace", "/DeviceGray")
    if footer:
        page.insert_text((10, 138), footer, fontsize=6)
    if drawing:
        page.draw_rect(pymupdf.Rect(10, 10, 40, 40), color=(0, 0, 0))


def _inline_colour_page(doc):
    """A 2 x 2 inch page whose only mark is an inline (BI … EI) 200 dpi RGB image."""
    page = doc.new_page(width=144, height=144)
    page.insert_text((10, 10), " ")  # gives the page a content stream to replace
    hexdata = (bytes([120, 30, 200]) * 400 * 400).hex().encode()
    image = b"BI /W 400 /H 400 /CS /RGB /BPC 8 /F /AHx ID " + hexdata + b"> EI"
    doc.update_stream(page.get_contents()[0], b"q 144 0 0 144 0 0 cm " + image + b" Q")


@pytest.mark.skipif(not can_ocr("eng"), reason=_NO_OCR)
def test_ocr_megapixels_match_what_ocrmypdf_renders(tmp_path):
    """The budget is only as good as its model of OCRmyPDF: text lifts a page
    to 400 dpi, a drawing also to colour, an ICC scan is colour. Compare with
    the PNG OCRmyPDF itself writes, so an upgrade that changes it goes red."""
    doc = pymupdf.open()
    _grey_scan_page(doc)  # 200 dpi, grey
    _grey_scan_page(doc, footer="Digitalizado com ScanApp")  # 400 dpi, grey
    _grey_scan_page(doc, drawing=True)  # 400 dpi, colour
    icc_grey = doc.new_page(width=144, height=144)  # 300 dpi, ICC grey = colour
    icc_grey.insert_image(icc_grey.rect, pixmap=_grey_pixmap(600))
    _inline_colour_page(doc)  # 200 dpi, colour: get_images lists no inline image
    source = tmp_path / "in.pdf"
    doc.save(source)
    work = tmp_path / "work"
    work.mkdir()
    command = ["ocrmypdf", "-k", "--redo-ocr", "--output-type", "pdf", "-l", "eng"]
    result = subprocess.run(
        [*command, source, tmp_path / "out.pdf"],
        capture_output=True,
        text=True,
        env={**os.environ, "TMPDIR": str(work)},
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-500:]
    for pno, page in enumerate(pymupdf.open(source), start=1):
        (raster,) = work.glob(f"*/{pno:06d}_rasterize.png")
        with Image.open(raster) as im:
            rendered = im.width * im.height / 1e6 * (1 if im.mode in ("L", "1", "P") else 2)
        predicted = pdf_tools._ocr_megapixels(page)
        assert predicted == pytest.approx(rendered, rel=0.02), (pno, im.mode, im.size)


def test_ocr_says_when_there_is_nothing_to_recognise(client, monkeypatch):
    """ENGINE-07 / CONTRACT-05 (P1): a born-digital PDF came back unchanged with
    a 200 — and was charged."""
    monkeypatch.setattr(pdf_tools, "_run_command", _no_tool)
    response = _post(client, "ocr", _text_pdf(["Texto já selecionável. " * 5] * 6))
    assert response.status_code == 422
    assert _error(response)["code"] == "already_searchable"


@pytest.mark.skipif(not can_ocr("por"), reason=_NO_OCR)
def test_ocr_reads_a_scan_that_has_a_small_text_footer(client):
    """APICODE-06 / ENGINE-07 / CONTRACT-05 (P1): --skip-text skipped any page
    with real text, so a scanner's footer left the body unsearchable (200)."""
    pdf = _scan(
        ["Relatório anual da associação cultural", "Contas aprovadas em assembleia geral"],
        footer="Digitalizado com ScanApp",
    )
    response = _post(client, "ocr", pdf, {"language": "portuguese"})
    assert response.status_code == 200
    assert "assembleia" in _all_text(response.content).lower()


# --- Convert ---------------------------------------------------------------

RTF = b"{\\rtf1\\ansi\\deff0 {\\fonttbl {\\f0 Arial;}}\\f0 Isto e RTF, nao DOCX.\\par}"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.parametrize(("name", "mime"), [("x.docx", DOCX_MIME), ("foto.png", "image/png")])
def test_convert_never_hands_other_formats_to_libreoffice(client, monkeypatch, name, mime):
    """APICODE-09 (P1): the type came from the name/MIME and LibreOffice
    content-sniffed, so RTF (and DOC, ODT, HTML) dressed as DOCX or PNG
    converted through every import filter."""
    monkeypatch.setattr(pdf_tools, "_run_command", _no_tool)
    response = _post(client, "convert", RTF, name=name, mime=mime)
    assert response.status_code == 422
    assert _error(response)["code"] == "invalid_document"


def test_convert_pins_the_libreoffice_import_filter(client, monkeypatch):
    """APICODE-09 (P1): soffice ran without --infilter."""
    from docx import Document

    seen = []

    def capture(command, **_kwargs):
        seen.append(command)
        raise ApiError(599, "captured", "captured")

    monkeypatch.setattr(pdf_tools, "_run_command", capture)
    buf = io.BytesIO()
    Document().save(buf)
    _post(client, "convert", buf.getvalue(), name="relatorio.docx", mime="application/octet-stream")
    assert seen and "--infilter=MS Word 2007 XML" in seen[0]


def _icc_bytes() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def test_convert_keeps_every_frame_of_a_tiff_with_an_icc_profile(client):
    """APICODE-12 / ENGINE-12 (P1): the ICC re-encode kept frame 0 only, so a
    3-page scanner TIFF became a 1-page PDF, silently."""
    frames = [Image.new("RGB", (200, 280), color) for color in ("red", "green", "blue")]
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="TIFF",
        save_all=True,
        append_images=frames[1:],
        compression="tiff_lzw",
        icc_profile=_icc_bytes(),
        dpi=(150, 150),
    )
    response = _post(client, "convert", buf.getvalue(), name="scan.tiff", mime="image/tiff")
    assert response.status_code == 200
    with pymupdf.open(stream=response.content, filetype="pdf") as doc:
        assert doc.page_count == 3


def test_convert_applies_exif_rotation_to_a_photo_with_an_icc_profile(client):
    """ENGINE-12 / CONTRACT-12 (P1): the ICC re-encode dropped EXIF orientation,
    so a portrait phone photo came out sideways."""
    image = Image.new("RGB", (400, 300), "white")  # stored landscape
    exif = image.getexif()
    exif[0x0112] = 6  # display rotated 90° -> portrait
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif.tobytes(), icc_profile=_icc_bytes())
    response = _post(client, "convert", buf.getvalue(), name="foto.jpg", mime="image/jpeg")
    assert response.status_code == 200
    with pymupdf.open(stream=response.content, filetype="pdf") as doc:
        assert doc[0].rect.height > doc[0].rect.width


def test_convert_accepts_a_real_image_behind_a_generic_mime_type(client):
    """Decision 14 / CONTRACT-17: accept the six formats when the browser sends
    a generic type — decided by the bytes."""
    buf = io.BytesIO()
    Image.new("RGB", (50, 50), "blue").save(buf, format="JPEG")
    response = _post(
        client, "convert", buf.getvalue(), name="foto", mime="application/octet-stream"
    )
    assert response.status_code == 200


# --- PDF to Word / Excel ---------------------------------------------------


def _docx_text(data: bytes) -> str:
    from docx import Document

    return " ".join(p.text for p in Document(io.BytesIO(data)).paragraphs)


def test_pdf_to_word_reads_an_ocr_text_layer(client):
    """ENGINE-08 (P1): after our own OCR, PDF→Word still said «use o OCR
    primeiro» — pdf2docx ignores invisible text."""
    lines = [f"Cláusula {i}: o contraente obriga-se a pagar a renda mensal." for i in range(8)]
    response = _post(client, "pdf-to-word", _scan(lines, ocr_layer=True))
    assert response.status_code == 200
    assert "renda mensal" in _docx_text(response.content)


def test_pdf_to_word_keeps_text_on_rotated_pages(client):
    """ENGINE-09 (P1): pages shown with /Rotate 90 or 180 lost all their text."""
    doc = pymupdf.open()
    for rotation in (0, 90, 180):
        page = doc.new_page()
        page.insert_text((72, 90), f"Tabela rodada {rotation} graus com totais anuais", fontsize=14)
        page.set_rotation(rotation)
    response = _post(client, "pdf-to-word", doc.tobytes())
    assert response.status_code == 200
    text = _docx_text(response.content)
    assert all(f"rodada {r} graus" in text for r in (0, 90, 180))


def test_pdf_to_excel_survives_control_characters(client, monkeypatch):
    """APICODE-13 (P1): one control character in any cell (a broken ToUnicode
    map) failed the whole file with 500."""
    from openpyxl import load_workbook

    monkeypatch.setattr(
        pymupdf.table.Table,
        "extract",
        lambda self: [["ref\x01A7", "1.234,56", "05/01/2024", "007"]],
    )
    response = _post(client, "pdf-to-excel", g.tables_pdf())
    assert response.status_code == 200
    ws = load_workbook(io.BytesIO(response.content)).worksheets[0]
    assert ws["A1"].value == "refA7"
    # ENGINE-21 (P2): unambiguous numbers and dates are numbers and dates.
    assert ws["B1"].value == 1234.56
    assert ws["C1"].value.date().isoformat() == "2024-01-05"
    assert ws["D1"].value == "007"


def test_in_process_tools_answer_504_at_the_time_budget(client, monkeypatch):
    """APICODE-11 (P1): 30 table pages took 83 s in-process with no deadline;
    TudoPDF gives up at 50 s while the thread kept working."""
    monkeypatch.setattr(pdf_tools, "PROCESSING_BUDGET_SECONDS", -1, raising=False)
    response = _post(client, "pdf-to-excel", g.tables_pdf())
    assert response.status_code == 504
    assert _error(response)["code"] == "processing_timeout"


# --- Protect / damaged input / repair --------------------------------------


@pytest.mark.parametrize(
    ("password", "status"), [("a" * 127, 200), ("a" * 128, 400), ("€" * 43, 400)]
)
def test_protect_refuses_passwords_no_reader_can_type(client, sample_pdf, password, status):
    """CONTRACT-03 (P1): above 127 UTF-8 bytes every reader truncates, so the
    protected file could never be opened again."""
    response = _post(client, "protect", sample_pdf, {"userPassword": password})
    assert response.status_code == status
    if status == 400:
        assert _error(response)["code"] == "password_too_long"
    else:
        with pikepdf.open(io.BytesIO(response.content), password=password) as pdf:
            assert len(pdf.pages) == 1


def test_a_truncated_pdf_is_refused_not_half_processed(client):
    """ENGINE-14 (P1): a 20-page file cut in half came back as 13 pages with a
    200 (protect), or as a 400/500 depending on the tool."""
    full = _text_pdf(["Relatório — página com conteúdo"], pages=20)
    with pymupdf.open(stream=full, filetype="pdf") as doc:
        full = doc.tobytes(use_objstms=False)
    cut = full[: int(len(full) * 0.55)]
    for endpoint, options in (
        ("protect", {"userPassword": "x"}),
        ("compress", {}),
        ("flatten", {}),
    ):
        response = _post(client, endpoint, cut, options)
        assert (endpoint, response.status_code, _error(response)["code"]) == (
            endpoint,
            422,
            "damaged_pdf",
        )


@pytest.mark.skipif(not pdfa_resources_present(), reason=_NO_GS)
def test_repair_says_when_nothing_was_recovered(client):
    """PAIDUI-03 (P1): a header plus random bytes came back as one blank page,
    «PDF reparado por reinterpretação», for 0,79 €."""
    garbage = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n" + os.urandom(3000)
    response = _post(client, "pdf-repair", garbage)
    assert response.status_code == 422
    assert _error(response)["code"] == "unrecoverable_pdf"


# --- HTTP layer ------------------------------------------------------------


def test_unauthenticated_upload_is_refused_before_the_body_is_read(client, monkeypatch):
    """APICODE-15 (P2): the whole upload was parsed and spooled (RAM-backed
    /tmp) before the key was checked, and before the 413."""
    from starlette.formparsers import MultiPartParser

    monkeypatch.setenv("API_KEY", "secret")
    parsed = []
    original = MultiPartParser.parse

    async def spy(self, *args, **kwargs):
        parsed.append(1)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(MultiPartParser, "parse", spy)
    response = client.post(
        "/v2/compress", files={"file": ("a.pdf", io.BytesIO(b"%PDF-1.7" + b"0" * 200_000))}
    )
    assert response.status_code == 401
    assert parsed == []

    monkeypatch.setenv("MAX_UPLOAD_BYTES", "1024")
    response = client.post(
        "/v2/compress",
        headers={"X-API-Key": "secret"},
        files={"file": ("a.pdf", io.BytesIO(b"%PDF-1.7" + b"0" * 2_000_000))},
    )
    assert response.status_code == 413
    assert _error(response)["code"] == "file_too_large"
    assert parsed == []


def test_deeply_nested_options_are_a_client_error(client, sample_pdf):
    """APICODE-24 (P3): 100k '[' raised RecursionError -> 500 internal_error."""
    response = client.post(
        "/v2/compress",
        files={"file": ("a.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": "[" * 100_000},
    )
    assert response.status_code == 400
    assert _error(response)["code"] == "invalid_options"


def test_filenames_lose_bidi_controls_and_dot_names(client, sample_pdf):
    """APICODE-29 (P3): U+202E survived into filename*, and '..' passed through."""
    from app.http_utils import sanitize_filename

    assert "‮" not in sanitize_filename("file‮gnp.exe.pdf", "output.pdf")
    assert sanitize_filename("..", "output.pdf") == "output.pdf"


def test_zip_page_names_sort_in_page_order(client):
    """ENGINE-28 (P3): pagina-10 sorted before pagina-2."""
    options = {"pages": "all", "format": "jpeg"}
    response = _post(client, "pdf-to-image", _text_pdf(["x"], pages=10), options)
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert names == sorted(names) and names[0] == "pagina-01.jpg"


def test_zip_of_page_images_is_compressed(client):
    """ENGINE R3 (P3): stored, not deflated, the ZIP of a text document's JPG
    pages grew 37-175% — a document page is mostly white, and deflate still
    finds it."""
    options = {"pages": "all", "format": "jpeg"}
    response = _post(client, "pdf-to-image", _text_pdf(["Relatório"] * 20, pages=3), options)
    entries = zipfile.ZipFile(io.BytesIO(response.content)).infolist()
    assert {entry.compress_type for entry in entries} == {zipfile.ZIP_DEFLATED}
    assert len(response.content) < sum(entry.file_size for entry in entries)


@pytest.mark.skipif(not has_soffice(), reason="needs LibreOffice")
def test_convert_a_broken_docx_is_a_user_error(client):
    """ENGINE-22 / APICODE-23 (P2): a corrupt DOCX answered 500 with
    LibreOffice's stderr in `details`."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<Types><Override ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        z.writestr("word/document.xml", "<w:document>not really</w")
    response = _post(client, "convert", buf.getvalue(), name="x.docx", mime=DOCX_MIME)
    assert response.status_code in (422, 500)
    assert _error(response)["details"] is None
