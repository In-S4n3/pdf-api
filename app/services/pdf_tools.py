"""Shared PDF processing services for v1 and v2 routes."""

from __future__ import annotations

import hashlib
import heapq
import io
import json
import logging
import math
import os
import re as _re
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections import Counter, deque
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import img2pdf
import pymupdf
import regex

from app.api_errors import ApiError
from app.config import get_settings

logger = logging.getLogger(__name__)

# Colour-managed conversions. Without ICC, MuPDF converts CMYK with the naive
# formula and a converted photo comes out visibly off (see _images_to_rgb).
# Set once at import and never toggled: MuPDF's context is process-global.
pymupdf.TOOLS.set_icc(True)

MAX_PAGES = 200  # coarse guard; each tool also has a time budget below
# Real files nest the page tree a handful of levels. qpdf walks it recursively
# inside this process and overflowed its stack between 10 000 and 20 000 (SIGSEGV).
MAX_PAGE_TREE_DEPTH = 1_000
# Page-tree objects _open_pdf walks itself, at ~20 µs each; a bigger tree (a
# 20 MB /Kids holds millions) is counted by qpdf in a child process.
MAX_PAGE_TREE_WALK = 5_000
TOOL_SUBPROCESS_TIMEOUT = 45
REDACTION_SCAN_TIMEOUT_SECONDS = 40
# In-process work (PyMuPDF/openpyxl loops) gets the same ceiling as the scan:
# under the 45 s subprocess cap and TudoPDF's 50 s wait, so the caller always
# hears a typed 504 instead of a dropped connection.
PROCESSING_BUDGET_SECONDS = 40

# Pixel budgets. A 1 KB PDF can declare a 5000 × 5000 pt page, and 300 dpi of
# that is 2.25 Gpx — the process is OOM-killed before any Python except runs.
# 40 Mpx ≈ 120 MB of RGB; A2 at 300 dpi is 34.8 Mpx, so every real page up to
# A2 still renders at full resolution.
MAX_RENDER_PIXELS = 40_000_000
# One embedded image. A0 scanned at 300 dpi is 139 Mpx; a 1-bit Flate image
# can declare 10 Gpx in a few KB and decode to gigabytes.
MAX_IMAGE_PIXELS = 150_000_000
# Cloud Run refuses a non-streamed HTTP/1 response above 32 MiB — after all
# the work is done. Refuse at 30 MiB with a sentence the user can act on.
MAX_RESPONSE_BYTES = 30 * 1024 * 1024

TIMEOUT_MESSAGE = "O processamento excedeu o tempo limite. Tente com um ficheiro mais pequeno."
TOOL_UNAVAILABLE_MESSAGE = (
    "Esta ferramenta está temporariamente indisponível. Tente novamente dentro de alguns minutos."
)
PASSWORD_PROTECTED_MESSAGE = (
    "Este PDF está protegido por palavra-passe. "
    "Desbloqueie-o primeiro com a ferramenta Desbloquear PDF."
)
INVALID_PDF_MESSAGE = "Não foi possível abrir o PDF. Verifique se o ficheiro é válido."
DAMAGED_PDF_MESSAGE = (
    "Este PDF está danificado e só foi possível ler parte dele. "
    "Use primeiro a ferramenta Reparar PDF."
)

# pdf_to_xlsx caps (in-process; see 2026-07-02-pdf-para-excel-design.md §3.2).
MAX_TABLES = 200  # cap total worksheets
MAX_CELLS = 500_000  # cap total cells written — bounds the in-memory openpyxl workbook
MAX_PATHS_PER_PAGE = 5000  # reject vector-graphics-bomb pages before find_tables clustering

OFFICE_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/tiff",
}

MIME_TO_EXT = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}

EXTENSION_TO_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

LANGUAGE_MAP = {
    "english": "eng",
    "spanish": "spa",
    "french": "fra",
    "german": "deu",
    # por alone read every «@» as "(D"/"G": 0 of 5 emails, 0 «@» on a 20-page
    # scan; por+eng: 3 of 5 and 1 040 «@», accents identical, +0-53 % time.
    "portuguese": "por+eng",
    "italian": "ita",
    "chinese": "chi_sim",
    "jpn": "jpn",
}

# --redo-ocr, not --skip-text: a scan with any real text (a scanner's footer)
# was skipped whole and came back unsearchable with a 200. No --deskew/--clean
# (redo refuses --deskew; --deskew also re-encoded every page image to RGB,
# ~5x the size). --rotate-pages fixes sideways scans (+21-56% time, included in
# the cap). One worker per vCPU: os.cpu_count() reports the host's cores, and
# ocrmypdf started 10 workers on 2 CPUs (~1 GB, thrashing). A test holds
# OCR_JOBS equal to --cpu in .github/workflows/deploy.yml and service.yaml.
OCR_JOBS = 4
OCR_FLAGS = ("--output-type", "pdf", "--optimize", "0", "--jobs", str(OCR_JOBS), "--rotate-pages")
# OCR alone gets a long budget: real 25-page scans were refused at 8 pages, and
# TudoPDF waits longer for OCR only (proxy 280 s → browser 290 s → Vercel 300 s;
# Cloud Run timeoutSeconds 285, which a test holds above this). Every other tool keeps
# TOOL_SUBPROCESS_TIMEOUT. The 80 s left cover cold start (~25 s), upload,
# the checks before ocrmypdf and TudoPDF's preview.
OCR_SUBPROCESS_TIMEOUT = 200
# The pages cap alone let 6 photo pages with captions through: OCRmyPDF renders
# any page with text or a vector drawing — a scanner's footer is enough — at
# 400 dpi, and the time goes into writing those pixels as PNG, twice per page.
# So the pixels it will render are budgeted too, colour counting twice
# (_ocr_megapixels). A page never splits and waits for a free worker, so the
# busiest worker sets the time: it may render MAX_OCR_MEGAPIXELS / OCR_JOBS.
# Seconds of processing on Cloud Run gen2, 4 vCPU / 4 GiB, 2026-09-24
# (Mpx total; busiest worker):
#   8 grey A4 scans (69.6; 17.4) 17 · 8 colour A4 scans (139.3; 34.8) 28.5
#   4 A4 photos with captions (123.7; 30.9) 39-40 · 8 A4 photos at 300 dpi (139.3; 34.8) ~45
# Gen1 on 2 vCPU ran ~3x slower than the 2 CPU bench box and cut 8 colour scans
# at 45 s; gen2 alone gained only 11-20 %.
# Those pages carry little text. Pages dense with text (~600 words, 300 dpi),
# 2026-10-08 on an M4 under docker --cpus=4 --memory=4g, /tmp in RAM:
#   grey 25 pages 35 s · 60 pages 66 s · colour 16 pages 27 s · 28 pages 41 s
# The two cases above ran 4.2x (photos, 9.5 s) and 5.0x (colour, 5.7 s) slower
# on Cloud Run than there, so x5. Tesseract sets the time on such pages: a grey
# page costs nearly a colour one, so pixels alone under-price it.
# Sizing at x5, 10 % under the kill (180 s):
#   colour: 41 x 5 / 121.8 Mpx busiest = 1.67 s/Mpx, slower than photos (1.29)
#     180 / 1.67 = 108 Mpx per worker → 105 → MAX_OCR_MEGAPIXELS = 4 x 105 = 420
#     (24 colour A4 scans, 6 rounds; 12 photo pages)
#   grey: 35 x 5 / 7 rounds (25 pages) = 25 s per round
#     180 / 25 = 7.2 rounds → 7 → MAX_OCR_PAGES = 4 x 7 = 28 (1-bit scans too)
# The 1.29 s/Mpx alone gave 60 pages / 540 Mpx: 60 grey pages ≈ 330 s here.
MAX_OCR_MEGAPIXELS = 420
MAX_OCR_PAGES = 28
# A page costs a round of Tesseract however few pixels it has, so it counts at
# least its share of the page cap (15 Mpx). Without that floor one A3 photo plus
# 27 grey pages fit both caps, yet left three workers nine grey pages each
# (~225 s), past the kill.
OCR_PAGE_FLOOR_MEGAPIXELS = MAX_OCR_MEGAPIXELS / MAX_OCR_PAGES
# Hand Tesseract at most ~A4 at 300 dpi; the 400 dpi renders above it only
# cost time (31 → 25 s on those photo pages). Scans up to 300 dpi are untouched.
OCR_DOWNSAMPLE_FLAGS = (
    "--tesseract-downsample-large-images",
    "--tesseract-downsample-above",
    "3600",
)
# When Tesseract gives up on a page, ocrmypdf ships that page without text and
# calls the job a success. Its timeout at the subprocess kill means a slow page
# ends the whole job with our typed timeout instead.
OCR_TESSERACT_TIMEOUT_FLAGS = ("--tesseract-timeout", str(OCR_SUBPROCESS_TIMEOUT))

CONFORMANCE_MAP = {
    "pdfa-1b": "1",
    "pdfa-2b": "2",
    "pdfa-3b": "3",
}

REGEX_TIMEOUT_SECONDS = 0.5
_REGEX_TOO_SLOW_MESSAGE = "O padrão regex é demasiado complexo (possível ReDoS). Simplifique-o."
_REDACT_AS_IMAGES = (
    "Converta-o em imagens com PDF para Imagem, junte-as num PDF com Converter para "
    "PDF, passe-o pelo OCR PDF e censure o resultado."
)

# \w, not [a-zA-Z]: "joão@exemplo.pt" is an address too, and with an ASCII
# class the \b before it could never fire after the «ã».
EMAIL_PATTERN = r"\b[\w.%+\-]+@[\w.\-]+\.[^\W\d_]{2,}\b"
# Real phone shapes only (PT, BR, international). The old catch-all
# `\d{1,4}[\d\s./-]{6,14}\d` crossed line breaks and took 456 table figures,
# dates, amounts and NIFs for phones in a 3-page contract. Separators are
# space, NBSP or hyphen — never a newline — and the number may not continue
# a longer figure on either side («1 234 567,89», an IBAN group).
_PHONE_SEP = r"[ \u00a0-]"
PHONE_PATTERN = (
    r"(?<![\w@+/,-])(?<![\d.]\.)(?<!\d[ \u00a0])"  # «Tel.912…» may start a match
    r"(?:"
    # +351 912 345 678 · 00351912345678 · (+351) 21 234 5678 · +55 (11) 91234-5678
    r"\(?(?:\+|00[ \u00a0]?)(?=(?:[ \u00a0()-]*\d){8})[1-9]\d{0,2}\)?"
    rf"(?:{_PHONE_SEP}?\(\d{{1,4}}\){_PHONE_SEP}?\d{{2,5}}(?:{_PHONE_SEP}?\d{{2,5}}){{1,3}}"
    rf"|(?:{_PHONE_SEP}?\d{{1,5}}){{1,2}}(?:{_PHONE_SEP}?\d{{2,5}}){{2,4}})"
    r"|\(\d{2,3}\)[ \u00a0]?(?:9[ \u00a0])?\d{4,5}[ \u00a0-]?\d{4}"  # BR: (11) 9 1234-5678
    r"|\d{2}[ \u00a0](?:9[ \u00a0])?\d{4,5}[ \u00a0-]\d{4}|\d{2}[ \u00a0]9\d{8}|9\d{4}-\d{4}"  # BR
    rf"|[29]\d{{2}}(?:{_PHONE_SEP}?\d{{3}}){{2}}|2\d{_PHONE_SEP}\d{{3}}{_PHONE_SEP}\d{{4}}"  # PT
    rf"|[29]\d{_PHONE_SEP}\d{{3}}(?:{_PHONE_SEP}\d{{2}}){{2}}|[29]\d{{2}}(?:{_PHONE_SEP}\d{{2}}){{3}}"
    r"|0[3589]00[ \u00a0]?\d{3}[ \u00a0]?\d{4}"  # BR: 0800 123 4567
    r"|(?:80[08]|70[78]|76[01])(?:[ \u00a0]?\d{3}){2}"  # PT: 800 200 200
    r")"
    r"(?![\w@/]|[.,-]?\d|[ \u00a0]\d)"
)
PATTERNS = {
    "email": EMAIL_PATTERN,
    "phone": PHONE_PATTERN,
}

VALID_REDACTION_STRATEGIES = ("email", "phone", "custom", "regex")
MAX_REGEX_LENGTH = 500
# The preview lists at most this many matches. Apply redacts every match past
# it too: the user never saw those, so could not have deselected them.
PREVIEW_MATCH_CAP = 5000

# MuPDF is not thread-safe and set_small_glyph_heights is process-global, so a
# preview scan and an apply never overlap (an abandoned thread from a timed-out
# request would otherwise compute different boxes, and different ids).
REDACTION_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class RedactionMatch:
    id: str
    page: int            # 0-indexed
    bbox: tuple[float, float, float, float]  # (x0, y0, x1, y1) in PDF points
    kind: str            # 'email' | 'phone' | 'custom' | 'regex'
    context: str         # word text inside the bbox (visible in UI)
    full_match: str      # the entire regex match (may span multiple words)


def _make_match(
    strategy: str,
    page_idx: int,
    bbox: tuple[float, float, float, float],
    context: str,
    full_match: str,
) -> RedactionMatch:
    """Build a RedactionMatch with a deterministic 16-char hex ID.

    Same (strategy, page, bbox, context) -> same ID, so a confirmed-IDs
    round-trip from frontend to backend continues to identify the same
    matches even though the helper is called twice (once for preview,
    once for apply).
    """
    digest = hashlib.sha1(
        f"{strategy}|{page_idx}|{bbox[0]:.2f},{bbox[1]:.2f},{bbox[2]:.2f},{bbox[3]:.2f}|{context}".encode()
    ).hexdigest()[:16]
    return RedactionMatch(
        id=digest, page=page_idx, bbox=bbox, kind=strategy,
        context=context, full_match=full_match,
    )


def _compile_pattern(
    strategy: str, custom_text: str, regex_pattern: str
) -> tuple[str, int]:
    """Validate inputs and return (pattern, flags). Raises ApiError on bad input."""
    if strategy not in VALID_REDACTION_STRATEGIES:
        raise ApiError(400, "invalid_redaction_strategy", "Estratégia de censura inválida.")

    if strategy == "custom":
        if not custom_text.strip():
            raise ApiError(400, "missing_custom_text", "Texto personalizado é obrigatório.")
        # Any whitespace between the words: a phrase that wraps a line, or is
        # joined by a non-breaking space, is still the phrase the user typed.
        return r"\s+".join(regex.escape(word) for word in custom_text.split()), regex.IGNORECASE

    if strategy == "regex":
        if not regex_pattern.strip():
            raise ApiError(400, "missing_regex_pattern", "Padrão regex é obrigatório.")
        if len(regex_pattern) > MAX_REGEX_LENGTH:
            raise ApiError(
                400, "regex_too_long",
                f"Padrão regex demasiado longo (máx {MAX_REGEX_LENGTH} caracteres).",
            )
        try:
            regex.compile(regex_pattern)
        except regex.error as exc:
            raise ApiError(400, "invalid_regex_pattern", "Padrão regex inválido.") from exc
        return regex_pattern, 0

    return PATTERNS[strategy], 0


def _check_deadline(deadline: float, message: str = TIMEOUT_MESSAGE) -> None:
    if time.monotonic() >= deadline:
        raise ApiError(504, "processing_timeout", message)


_SCAN_TIMEOUT_MESSAGE = "A pesquisa de dados sensíveis excedeu o tempo limite."


def _iter_matches(
    doc,
    *,
    strategy: str,
    custom_text: str,
    regex_pattern: str,
    deadline: float | None = None,
) -> Iterator[RedactionMatch]:
    """Yield bounded redaction matches for both preview and apply.

    The order is deterministic (page, then first appearance in the text) so the
    preview and a later apply — possibly in another process — enumerate the
    same matches in the same order; ids repeat only for the same box.
    """
    if doc.needs_pass:
        raise ApiError(400, "password_protected_pdf", PASSWORD_PROTECTED_MESSAGE)
    if doc.page_count > MAX_PAGES:
        raise ApiError(
            422,
            "too_many_pages",
            f"O PDF tem demasiadas páginas para censurar (máximo: {MAX_PAGES}).",
        )

    pattern_str, flags = _compile_pattern(strategy, custom_text, regex_pattern)
    compiled_pattern = regex.compile(pattern_str, flags)
    if deadline is None:
        deadline = time.monotonic() + REDACTION_SCAN_TIMEOUT_SECONDS

    # Sticky notes, form fields and stamps keep text outside the page content,
    # where neither get_text() nor apply_redactions() looks: a redacted email
    # survived in a note's /Contents and in a field's appearance stream. Baking
    # turns every annotation and widget appearance into page content (a note's
    # hidden /Contents is dropped), so preview, ids and removal see one text.
    # Two kinds are not baked: a print-only (NoView) mark or form field would
    # show on screen, and a /Redact mark another editor left pending means
    # "remove this" — baked, it became an outline over text that stays readable.
    no_view = pymupdf.PDF_ANNOT_IS_NO_VIEW
    pymupdf.TOOLS.set_small_glyph_heights(True)
    try:
        marked = []
        for page in doc:
            for annot in [a for a in page.annots() if a.flags & no_view]:
                page.delete_annot(annot)
            for field in [w for w in page.widgets() if w._annot.flags & no_view]:
                page.delete_widget(field)  # page.annots() skips form fields
            marks = list(page.annots(types=(pymupdf.PDF_ANNOT_REDACT,)))
            if marks:
                # Applying the mark decodes the images under it: budget first, or a
                # 32 KB file peaks at 605 MiB, in a preview that costs no free use.
                _check_image_budget(doc, pages=[page.number])
                for mark in marks:
                    _prepare_pending_mark(page, mark)
                marked.append(page.number)
        page = marks = mark = None  # reload_page refuses a page something else holds
        _apply_redactions(doc, marked, deadline, marks=True)
    finally:
        pymupdf.TOOLS.set_small_glyph_heights(False)
    doc.bake(annots=True, widgets=True)

    search_doc = _with_every_layer_on(doc)
    try:
        _check_text_in_fill_patterns(search_doc, compiled_pattern, deadline)
        form_pages: Counter | None = None
        pages_per_image: Counter | None = None
        seen_ids: set[str] = set()
        for page_idx, page in enumerate(search_doc):
            _check_deadline(deadline, _SCAN_TIMEOUT_MESSAGE)
            # What a reader extracts (MuPDF puts /ActualText in place of the
            # glyphs), then what the page draws: «ana at example dot com» as
            # replacement text hid the drawn ana@example.com from the matcher.
            text = page.get_text("text", flags=pymupdf.TEXTFLAGS_TEXT)
            views = [(0, text)]
            held = page.get_text("text", flags=_HELD, clip=pymupdf.INFINITE_RECT())
            if held != text:
                drawn = page.get_text("text", flags=pymupdf.TEXTFLAGS_TEXT | _DRAWN)
                if drawn != text:
                    views.append((_DRAWN, drawn))
                _check_text_outside_the_page(doc, page, compiled_pattern, drawn, held)

            trace = page.get_texttrace()
            if _unextracted_matches(page, trace, compiled_pattern):
                raise ApiError(
                    422,
                    "text_of_no_size",
                    "Este PDF tem texto escondido, desenhado sem tamanho, que não "
                    "conseguimos censurar com segurança. " + _REDACT_AS_IMAGES,
                )

            searches = [(view, n) for view, text in views for n in _needles(compiled_pattern, text)]
            if searches and form_pages is None:
                form_pages = Counter(x for p in doc for x in {f[0] for f in p.get_xobjects()})
                pages_per_image = Counter(
                    x for p in doc for x in {i[0] for i in p.get_images(full=True)})
            if searches:  # the apply refuses it on this page, after payment
                _check_image_budget(doc, pages=[page_idx])
            inline = _inline_images_in_shared_forms(doc, page_idx, form_pages) if searches else []
            in_pixels = _in_pixels(trace)
            images = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()] if searches else []
            covered = []  # boxes that may cover the match in an image's pixels too
            matched: dict = {}  # per view, read once the page has a hit
            for view, needle in searches:
                _check_deadline(deadline, _SCAN_TIMEOUT_MESSAGE)
                for rect in page.search_for(needle, flags=_SEARCH_FLAGS | view):
                    if view not in matched:
                        matched[view] = _where_it_matches(page, compiled_pattern, view, images)
                    words = matched[view](rect, needle)
                    if words == []:
                        continue
                    if in_pixels(rect):
                        if any(rect.intersects(image) for image in inline):
                            raise ApiError(
                                422,
                                "image_in_shared_form",
                                "Este PDF repete a mesma imagem em várias páginas através de um "
                                "modelo, e não a conseguimos censurar em todas. "
                                + _REDACT_AS_IMAGES,
                            )
                        covered.append(rect)
                    if words is None:  # no glyph under the hit: its words, or the hit itself
                        clipped = page.get_text(
                            "words", clip=rect, flags=pymupdf.TEXTFLAGS_WORDS | view)
                        words = (
                            [(w[0], w[1], w[2], w[3], w[4]) for w in clipped]
                            or [(rect.x0, rect.y0, rect.x1, rect.y1, needle)]
                        )
                    for x0, y0, x1, y1, word_text in words:
                        bbox = (x0, y0, x1, y1)
                        match = _make_match(strategy, page_idx, bbox, word_text, needle)
                        if match.id in seen_ids:
                            continue
                        seen_ids.add(match.id)
                        yield match
            if covered:
                _check_image_copies(doc[page_idx], covered, pages_per_image)
    finally:
        if search_doc is not doc:
            search_doc.close()


def _where_it_matches(page, compiled_pattern, view: int, images: list):
    """Where a search_for hit is the match: the box of each of its words, []
    if the hit is not the match, None if no glyph lies under it.

    search_for finds the needle inside more text too: the phone 912345678 cost
    the account 19123456780 its digits. The page is read glyph by glyph, the
    pattern run over all of it — a lookbehind may look at the line above — and
    a hit stands if a glyph under it lies in a match that holds the needle.
    By position: the phone drawn twice made two hits for two matches, one in
    the account; and an OCR'd page draws hidden text over its own, two copies
    under one hit. A match that holds the needle: «Ana Silva» found inside
    «Ana Silvano» stood on the match «Silvano». Not every glyph: a font kerns
    «V.», and the full stop's centre fell inside «ana@example.TV».

    The words are those glyphs: words clipped to the hit took pieces of the
    lines above and below where the leading is tight. A box covers each glyph
    as PyMuPDF places it and as MuPDF does: for a font whose ascender and
    descender span less than 1 em, PyMuPDF moves the box, and a Type3
    «Silva» drawn above its baseline kept every glyph. Then the box spares the
    glyphs around it (_spare_neighbours) — but not over an image, whose pixels
    it blanks too: moved off the OCR line above, it left the tops of the
    email's letters in the scan.
    """

    def lines_of(raw) -> list:
        return [line for block in raw["blocks"] for line in block.get("lines", ())]

    def chars_of(raw):
        return (c for line in lines_of(raw) for span in line["spans"] for c in span["chars"])

    def key(char) -> tuple:
        return char["c"], *char["origin"]

    placed = page.get_text("rawdict", flags=_SEARCH_FLAGS | view)
    pymupdf.TOOLS.unset_quad_corrections(True)
    try:  # the boxes MuPDF tests when it removes glyphs (pdf_redact_text_filter)
        native = page.get_text("rawdict", flags=_SEARCH_FLAGS | view)
    finally:
        pymupdf.TOOLS.unset_quad_corrections(False)
    # Paired by character and origin: the reads can differ. A glyph of no width
    # and no height is a span the second drops, and a glyph whose box misses
    # the page is dropped by the read whose box does. A glyph left unpaired
    # has no tested box, and a box over it is not trimmed.
    tested: dict = {}
    for char in chars_of(native):
        tested.setdefault(key(char), deque()).append(pymupdf.Rect(char["bbox"]))
    count = Counter(key(char) for char in chars_of(placed))
    tested = {k: boxes for k, boxes in tested.items() if len(boxes) == count[k]}
    lines, text, glyphs = [], [], []
    for line in lines_of(placed):
        start, line_box = len(text), pymupdf.Rect(line["bbox"])
        for char in (c for span in line["spans"] for c in span["chars"]):
            box = tested[key(char)].popleft() if key(char) in tested else None
            text.append(char["c"])
            glyphs.append((pymupdf.Rect(char["bbox"]), box))
            line_box |= box or line_box
        lines.append((line_box, start, len(text), not line["wmode"]))
        text.append("\n")
        glyphs.append(None)
    text = "".join(text)
    found, in_match = [], [None] * len(text)
    for m in _finditer(compiled_pattern, text):
        found.append(" ".join(m.group().split()).translate(_ASCII_LOWER))
        in_match[m.start():m.end()] = [len(found) - 1] * (m.end() - m.start())
    upright = {i for _, start, end, flat in lines if flat for i in range(start, end)}

    def known(held: set) -> bool:  # MuPDF's boxes of upright glyphs
        return held <= upright and all(glyphs[i][1] is not None for i in held)

    def words(rect, needle: str):
        hit = [i for box, start, end, _ in lines if box.intersects(rect)
               for i in range(start, end) if _centre(glyphs[i][0]) in rect]
        if not hit:
            return None
        needle = " ".join(needle.split()).translate(_ASCII_LOWER)
        mine = [i for i in hit if in_match[i] is not None and needle in found[in_match[i]]]
        held = set(mine)
        runs: list[list[int]] = []
        for i in mine:
            if text[i].isspace():
                continue
            if runs and runs[-1][-1] == i - 1:
                runs[-1].append(i)
            else:
                runs.append([i])
        if mine and not runs:  # whitespace alone: the words clipped to the hit
            return None
        boxes = []
        for run in runs:
            box = pymupdf.Rect(glyphs[run[0]][0])
            for i in run:
                box |= glyphs[i][0]
                box |= glyphs[i][1] or box
            if known(held) and not any(box.intersects(image) for image in images):
                others = [glyphs[j][1] for line_box, start, end, _ in lines
                          if line_box.intersects(box) for j in range(start, end)
                          if j not in held and glyphs[j][1] is not None and not text[j].isspace()]
                box = _spare_neighbours(box, [glyphs[i][1] for i in run], others)
            boxes.append((box.x0, box.y0, box.x1, box.y1, "".join(text[i] for i in run)))
        return boxes

    return words


def _centre(rect) -> pymupdf.Point:
    return pymupdf.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)


def _spare_neighbours(box, mine: list, others: list) -> pymupdf.Rect:
    """box, moved off the glyphs around the match that it would remove too.

    MuPDF removes every glyph whose box, 10% smaller on each side, touches a
    redaction (pdf_redact_text_filter, pdf-clean.c in MuPDF 1.27). A glyph's
    box runs from the font's ascender to its descender, taller than tight
    leading: «Silva» took «Cont» from the line above and «w» from «Below». A
    font kerns «V.»: the full stop's box starts inside the V's, and went too.

    Each side moves off a neighbour's whole box if it can, else off the part
    MuPDF tests, but never into the middle of a glyph of the match (40% of its
    width, 20% of its height): that glyph goes, whatever the box. A neighbour
    the box cannot spare that way stays under it.
    """
    core = (
        min(g.x0 + 0.3 * g.width for g in mine), min(g.y0 + 0.4 * g.height for g in mine),
        max(g.x1 - 0.3 * g.width for g in mine), max(g.y1 - 0.4 * g.height for g in mine),
    )
    edges = list(box)  # x0, y0, x1, y1
    for glyph in others:
        if not glyph.intersects(pymupdf.Rect(edges)):
            continue
        centre = _centre(glyph)
        past_w, past_h = 0.08 * glyph.width, 0.08 * glyph.height  # 2% past the tested part
        moves = []  # (side, its whole box, past the tested part)
        if centre.x < core[0]:
            moves.append((0, glyph.x1, glyph.x1 - past_w))
        if centre.y < core[1]:
            moves.append((1, glyph.y1, glyph.y1 - past_h))
        if centre.x > core[2]:
            moves.append((2, glyph.x0, glyph.x0 + past_w))
        if centre.y > core[3]:
            moves.append((3, glyph.y0, glyph.y0 + past_h))
        tries = [(s, whole) for s, whole, _ in moves] + [(s, part) for s, _, part in moves]
        for side, edge in tries:
            if side < 2 and edge <= core[side]:
                edges[side] = max(edges[side], edge)
                break
            if side >= 2 and edge >= core[side]:
                edges[side] = min(edges[side], edge)
                break
    return pymupdf.Rect(edges)


# The text a page draws, without /ActualText in place of its glyphs.
_DRAWN = pymupdf.TEXT_IGNORE_ACTUALTEXT
# All of it, also where no viewer shows it (off the page, outside the CropBox).
_HELD = (pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_MEDIABOX_CLIP) | _DRAWN
# search_for's own default flags, spelled out so a view can add _DRAWN.
_SEARCH_FLAGS = (
    pymupdf.TEXT_DEHYPHENATE
    | pymupdf.TEXT_PRESERVE_WHITESPACE
    | pymupdf.TEXT_PRESERVE_LIGATURES
    | pymupdf.TEXT_MEDIABOX_CLIP
)


def _occurrences(compiled_pattern, text: str) -> list[str]:
    """Every match in text, its whitespace collapsed to one space: search_for
    spans line breaks and non-breaking spaces, a literal "\\n" or "\\xa0" in
    the needle not."""
    found = (" ".join(match.group().split()) for match in _finditer(compiled_pattern, text))
    return [needle for needle in found if needle]


def _finditer(compiled_pattern, text: str) -> list:
    try:
        return list(compiled_pattern.finditer(text, timeout=REGEX_TIMEOUT_SECONDS))
    except regex.error as exc:
        raise ApiError(400, "invalid_regex_pattern", "Padrão regex inválido.") from exc
    except TimeoutError as exc:
        raise ApiError(400, "regex_too_slow", _REGEX_TOO_SLOW_MESSAGE) from exc


def _needles(compiled_pattern, text: str) -> list[str]:
    """First spelling of each match, in reading order. Variants in ASCII case
    collapse: search_for ignores it and would box the same place twice. It
    does not ignore «Ã»: collapsed into «João», «JOÃO» was never searched."""
    needles: dict[str, str] = {}
    for needle in _occurrences(compiled_pattern, text):
        needles.setdefault(needle.translate(_ASCII_LOWER), needle)
    return list(needles.values())


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _with_every_layer_on(doc):
    """doc, or a copy of it that draws every optional-content layer.

    MuPDF extracts only what is drawn, and a layer that is off draws nothing —
    yet any reader can switch it on, and its text was never redacted. Matching
    runs on the copy; the boxes apply to doc, where apply_redactions removes
    the hidden glyphs as well and the layer stays off.

    The copy has no /OCProperties at all, so MuPDF treats nothing as optional.
    Every layer on was not enough: a layer's own /Usage /ViewState /OFF still
    hid it, and content shown only while a layer is off (/P /AllOff) vanished
    from the copy — a visible email went unredacted.
    """
    if doc.xref_get_key(doc.pdf_catalog(), "OCProperties")[0] == "null":
        return doc
    with pymupdf.open(stream=doc.tobytes(), filetype="pdf") as copy:
        copy.xref_set_key(copy.pdf_catalog(), "OCProperties", "null")
        return pymupdf.open(stream=copy.tobytes(), filetype="pdf")  # MuPDF reads layers on open


def _clip_to_the_page(page) -> None:
    """Remove what is drawn wholly outside the CropBox; a glyph across the edge stays.

    MuPDF clips in PDF coordinates, unrotated. Page.clip_to_rect maps the rect
    the wrong way (page to PDF with the PDF-to-page matrix): on a CropBox that
    does not start at 0 0 it cut every visible glyph, and a turned page.rect
    cut visible text too.
    """
    rotation = page.rotation
    page.set_rotation(0)
    box = page.rect * ~page.transformation_matrix  # the CropBox, in PDF coordinates
    pymupdf.mupdf.pdf_clip_page(
        pymupdf.mupdf.pdf_page_from_fz_page(page.this), pymupdf.mupdf.FzRect(*box))
    page.set_rotation(rotation)


def _text_in_fill_pattern() -> ApiError:
    return ApiError(
        422,
        "text_in_fill_pattern",
        "Este PDF tem texto dentro de um padrão de preenchimento, onde não o "
        "conseguimos censurar com segurança. " + _REDACT_AS_IMAGES,
    )


def _check_text_in_fill_patterns(doc, compiled_pattern, deadline: float) -> None:
    """Refuse a match inside a tiling pattern's cell.

    MuPDF reports a cell's text but can neither place nor remove it: the box
    went somewhere else and the email stayed in the pattern. Each cell is read
    on its own, drawn as a form on a scratch page: a fill of no area painted
    nothing on the page, and its cell still held the email.
    """
    if not _tiling_cells(doc):
        return
    with pymupdf.open(stream=doc.tobytes(), filetype="pdf") as scratch:
        for xref in _tiling_cells(scratch):  # its own numbers, whatever the save did
            _check_deadline(deadline, _SCAN_TIMEOUT_MESSAGE)
            scratch.xref_set_key(xref, "Type", "/XObject")
            scratch.xref_set_key(xref, "Subtype", "/Form")
            page = scratch.new_page()
            contents = scratch.get_new_xref()
            scratch.update_object(contents, "<<>>")
            scratch.update_stream(contents, b"/Cell Do")
            scratch.xref_set_key(page.xref, "Resources", f"<</XObject<</Cell {xref} 0 R>>>>")
            scratch.xref_set_key(page.xref, "Contents", f"{contents} 0 R")
            if _held_matches(scratch.reload_page(page), compiled_pattern):
                raise _text_in_fill_pattern()


def _inline_images_in_shared_forms(doc, page_number: int, form_pages: Counter) -> list:
    """Where the page draws an inline image, if it draws a form XObject that
    other pages draw too and that holds one. MuPDF blanks the pixels under a
    box in a copy of the form for this page; the other pages kept the original,
    and nothing pairs a form with its copy (Fx became Fm1)."""
    page = doc[page_number]
    if not any(form_pages[x] > 1 and _holds_inline_image(doc, x) for x, *_ in page.get_xobjects()):
        return []
    return [pymupdf.Rect(i["bbox"]) for i in page.get_image_info(xrefs=True) if i["xref"] == 0]


def _holds_inline_image(doc, xref: int) -> bool:
    """Whether a form's content draws an inline image. Its operators, parsed:
    «(Monthly BI Report)» holds the bytes BI too. Sized in pieces first; too
    heavy to parse, or unparseable, counts as yes."""
    import pikepdf

    if doc.xref_get_key(xref, "Filter")[1] == "null":
        pieces = [doc.xref_stream_raw(xref)]
    elif (doc.xref_get_key(xref, "Filter")[1], doc.xref_get_key(xref, "DecodeParms")[0]) == (
        "/FlateDecode", "null"
    ):
        pieces = _inflate(doc.xref_stream_raw(xref))
    else:  # ponytail: other filters decode whole; rare in content streams
        pieces = [doc.xref_stream(xref)]
    if sum(len(piece) for piece in pieces) > _MAX_PARSED_CONTENT_BYTES:
        return True
    with pikepdf.new() as pdf:
        try:
            instructions = pikepdf.parse_content_stream(pdf.make_stream(doc.xref_stream(xref)))
        except Exception:
            return True
        return any(isinstance(i, pikepdf.ContentStreamInlineImage) for i in instructions)


def _tiling_cells(doc) -> list[int]:
    cells = []
    for xref in range(1, doc.xref_length()):
        with suppress(Exception):  # a number no xref section defines raises; it is null
            if doc.xref_get_key(xref, "PatternType")[1] == "1" and doc.xref_is_stream(xref):
                cells.append(xref)
    return cells


def _unextracted_matches(page, spans, compiled_pattern) -> list[str]:
    """Matches in what get_texttrace spans draw and get_text does not return:
    text at no size or flat on one axis (a zero font size, a flat matrix). No
    viewer shows it and the matcher never saw it, yet the file holds it.
    get_text("text") even split a flat email one character per line.

    Compared glyph by glyph wherever the spans hold a match. Counting matches
    was cheaper, but an email split across two fonts is two spans, and a copy
    at no size took its place in the count."""

    def text(chars) -> str:
        return "".join(chr(c[0]) if 0 < c[0] < 0x110000 else "\ufffd" for c in chars)

    if not _occurrences(compiled_pattern, "\n".join(text(s["chars"]) for s in spans)):
        return []
    raw = page.get_text("rawdict", flags=_HELD, clip=pymupdf.INFINITE_RECT())
    extracted = {
        (round(c["origin"][0], 1), round(c["origin"][1], 1))
        for block in raw["blocks"] for line in block.get("lines", ())
        for span in line["spans"] for c in span["chars"]
    }
    left = "\n".join(
        text(c for c in s["chars"]
             if not s["size"] or (round(c[2][0], 1), round(c[2][1], 1)) not in extracted)
        for s in spans
    )
    return _occurrences(compiled_pattern, left)


def _in_pixels(spans, *, marks: bool = False):
    """Whether a box may hold its match in an image's pixels too: it centres
    glyphs get_texttrace spans draw for nobody — invisible (3 Tr) or
    transparent, an OCR layer over a scan — and no visible glyph. A digital
    page run through OCR keeps a visible twin of each word, over a background.
    Another editor's mark needs no hidden glyph: a scan may have no OCR layer.

    Glyphs, not spans: labels in columns are one span with spaces between
    them, and its box took the OCR'd email between them for visible text.
    """

    def centres(kind) -> list:
        return [
            pymupdf.Point((c[3][0] + c[3][2]) / 2, (c[3][1] + c[3][3]) / 2)
            for s in spans if kind(s) for c in s["chars"]
            if not (0 < c[0] < 0x110000 and chr(c[0]).isspace())
        ]

    hidden = centres(lambda s: s["type"] == 3 or not s["opacity"])
    shown = centres(lambda s: s["type"] in (0, 1) and s["opacity"])
    return lambda box: (
        (marks or any(glyph in box for glyph in hidden))
        and not any(glyph in box for glyph in shown)
    )


def _held_matches(page, compiled_pattern) -> Counter:
    text = page.get_text("text", flags=_HELD, clip=pymupdf.INFINITE_RECT())
    found = _occurrences(compiled_pattern, text)
    found += _unextracted_matches(page, page.get_texttrace(), compiled_pattern)
    return Counter(n.casefold() for n in found)


def _check_text_outside_the_page(doc, page, compiled_pattern, drawn: str, held: str) -> None:
    """Remove matches no viewer shows, or refuse the file.

    Text off the page or below the CropBox is shown by no viewer, so nobody
    redacts it, yet every extractor returns it. MuPDF also reports the text of
    a tiling pattern's cell, where it cannot place or remove it: a box at the
    coordinates it gives blacked out unrelated text and left the email.
    """
    shown = Counter(n.casefold() for n in _occurrences(compiled_pattern, drawn))

    def outside(held: str) -> Counter:
        return Counter(n.casefold() for n in _occurrences(compiled_pattern, held)) - shown

    if not outside(held):
        return
    _clip_to_the_page(doc[page.number])
    if page.parent is not doc:
        _clip_to_the_page(page)
    if outside(page.get_text("text", flags=_HELD, clip=pymupdf.INFINITE_RECT())):
        raise _text_in_fill_pattern()


def _prepare_pending_mark(page, mark) -> None:
    """Ready a /Redact mark another editor left for apply_redactions.

    PyMuPDF writes the mark's overlay text in its /DA font and has metrics for
    the 14 base fonts (and its CJK ones) only: /ArialMT, as other editors write
    it, answered 500. Such a mark keeps its fill, without the text. And PyMuPDF
    paints the fill over the whole /Rect while MuPDF removes the text under the
    QuadPoints only: a mark over the end of one line and the start of the next
    blacked out both lines, the unmarked text still in the file. So a mark with
    quads becomes one mark per quad.
    """
    doc = page.parent
    try:  # _parse_da is how apply_redactions itself reads the font
        pymupdf.get_text_length("", pymupdf.TOOLS._parse_da(mark)[1])
    except ValueError:
        doc.xref_set_key(mark.xref, "OverlayText", "null")
    points = mark.vertices or []
    if len(points) < 4:  # no whole quad: MuPDF removes under the /Rect it fills
        return
    keys = _redact_keys(mark)
    for i in range(0, len(points) - 3, 4):
        _add_redaction(page, pymupdf.Quad(points[i : i + 4]).rect, keys)
    page.delete_annot(mark)


def _redact_keys(mark) -> dict:
    """What a /Redact mark paints in place of what it removes."""
    return {key: mark.parent.parent.xref_get_key(mark.xref, key)
            for key in ("IC", "DA", "OverlayText", "Q")}


def _add_redaction(page, rect, keys: dict) -> None:
    piece = page.add_redact_annot(rect, cross_out=False)
    for key, (kind, value) in keys.items():
        if kind != "null":
            page.parent.xref_set_key(
                piece.xref, key, pymupdf.get_pdf_str(value) if kind == "string" else value
            )


def _extract_matches(
    doc,
    *,
    strategy: str,
    custom_text: str,
    regex_pattern: str,
    deadline: float | None = None,
) -> list[RedactionMatch]:
    """Collect matches for apply; preview streams into its bounded payload."""
    return list(
        _iter_matches(
            doc,
            strategy=strategy,
            custom_text=custom_text,
            regex_pattern=regex_pattern,
            deadline=deadline,
        )
    )


def _trim_process_output(value: str, limit: int = 500) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[-limit:]


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the tool's whole process group, then reap the leader.

    `/usr/bin/soffice` execs oosplash, which forks soffice.bin; ocrmypdf forks
    workers that run tesseract, unpaper and gs. Killing only the direct child
    left all of those running after a 504, still holding their temp files.
    """
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=5)


def _spawn(
    command: list[str],
    *,
    timeout: float,
    tmpdir: str,
    mem_bytes: int | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run a tool in its own process group with TMPDIR inside the request's dir.

    Raises subprocess.TimeoutExpired (after killing the whole group) and
    FileNotFoundError; the caller's TemporaryDirectory then removes whatever the
    tool wrote — ocrmypdf's work dir used to stay behind in RAM-backed /tmp.
    """
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={**os.environ, "TMPDIR": tmpdir},
        **({"text": True, "encoding": "utf-8", "errors": "replace"} if text else {}),
    )
    if mem_bytes:
        # Set from the parent right after spawn, not in preexec_fn (unsafe in a
        # threaded server): the child is still starting, long before it decodes
        # anything. ponytail: Linux-only; on macOS dev the timeout is the cap.
        with suppress(AttributeError, OSError, ValueError):
            import resource

            resource.prlimit(proc.pid, resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException:
        _kill_group(proc)
        raise
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def _run_command(
    command: list[str],
    *,
    timeout: int,
    tmpdir: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return _spawn(command, timeout=timeout, tmpdir=tmpdir)
    except FileNotFoundError as exc:
        logger.error("tool missing: %s", command[0])
        raise ApiError(
            status_code=503,
            code="tool_unavailable",
            message=TOOL_UNAVAILABLE_MESSAGE,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ApiError(
            status_code=504,
            code="processing_timeout",
            message=TIMEOUT_MESSAGE,
        ) from exc


def _pdf_start(content: bytes) -> int:
    """Offset of the %PDF- header, or 400.

    Ghostscript picks its interpreter from the first bytes: a leading %!PS runs
    the upload as a PostScript program, even when %PDF- appears in a comment.
    """
    start = content.find(b"%PDF-", 0, 1024)
    if start < 0:
        raise ApiError(
            400,
            "invalid_pdf",
            "O ficheiro não é um PDF válido." if content else "O ficheiro está vazio.",
        )
    return start


def _open_pdf(content: bytes, *, hidden_pages_ok: bool = False):
    """Open an uploaded PDF, or refuse it with the error every tool shares.

    Not a PDF / unreadable → 400 invalid_pdf; open password → 400
    password_protected_pdf; no pages, only partly readable, or a page tree MuPDF
    misreads (see _check_page_tree) → 422 damaged_pdf.
    """
    _pdf_start(content)
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ApiError(400, "invalid_pdf", INVALID_PDF_MESSAGE) from exc
    try:
        if doc.needs_pass:
            raise ApiError(400, "password_protected_pdf", PASSWORD_PROTECTED_MESSAGE)
        try:
            empty = doc.page_count == 0  # MuPDF refuses a /Count above its object count
        except Exception as exc:
            raise ApiError(422, "damaged_pdf", DAMAGED_PDF_MESSAGE) from exc
        _check_page_tree(doc, content, hidden_pages_ok=hidden_pages_ok)
        if empty:
            if doc.is_repaired:
                raise ApiError(422, "damaged_pdf", DAMAGED_PDF_MESSAGE)
            raise ApiError(400, "invalid_pdf", "O PDF não tem páginas.")
        if doc.is_repaired:
            # MuPDF rebuilt a broken file. Where qpdf reads a different number
            # of pages (a truncated 60-page file: 60 vs 30) the result would be
            # half a document behind a success status. qpdf cannot open an
            # object-stream file that lost only its xref stream (None). And a
            # matching count proves nothing about content: a linearized file cut
            # inside its last page still counts every page.
            pages = _qpdf_page_count(content)
            if pages not in (None, doc.page_count) or _pages_with_content(doc) != doc.page_count:
                raise ApiError(422, "damaged_pdf", DAMAGED_PDF_MESSAGE)
    except BaseException:
        doc.close()
        raise
    return doc


def _check_page_tree(doc, content: bytes, *, hidden_pages_ok: bool = False) -> None:
    """422 when MuPDF would misread the page tree.

    MuPDF takes the number of pages from the tree's /Count; qpdf, Ghostscript and
    viewers follow /Kids. /Count 3 over 4 pages hid page 4 from every tool while
    the saved file still showed it: Censurar left its email readable (unless
    hidden_pages_ok). A /Kids entry MuPDF cannot open — null, a missing object, a
    node reached twice (a cycle) — still counts: a 500 on most tools. Walked
    without recursion, each /Pages node once, a repeated page counted each time;
    past MAX_PAGE_TREE_WALK objects qpdf counts instead, in its own process.
    """
    size, limit = doc.xref_length(), doc.page_count
    pages, walked, broken = 0, 0, False
    try:
        kind, root = doc.xref_get_key(doc.pdf_catalog(), "Pages")
        stack = [(int(root.split()[0]), 1)] if kind == "xref" else []
        nodes = set()
        while stack and not broken and pages <= limit and walked < MAX_PAGE_TREE_WALK:
            xref, times = stack.pop()
            walked += 1
            kind, kids = doc.xref_get_key(xref, "Kids") if 0 < xref < size else ("null", "")
            if kind == "null":
                # a page is a dictionary, not a stream: what qpdf and pdf.js show
                page = 0 < xref < size and not doc.xref_is_stream(xref) and doc.xref_get_keys(xref)
                broken, pages = not page, pages + times
            elif kind not in ("array", "xref") or xref in nodes:
                broken = True  # a node reached twice: a cycle
            else:
                nodes.add(xref)
                if kind == "xref":  # an indirect /Kids array
                    kids = doc.xref_object(int(kids.split()[0]))
                broken = bool(_re.sub(r"\d+ \d+ R|[\[\]\s]", "", kids))  # a null, a number
                refs = Counter(m[1] for m in _re.finditer(r"(\d+) \d+ R", kids))
                stack += [(int(kid), n) for kid, n in refs.items()]
    except Exception:
        return  # unreadable tree: the damaged-file checks after this decide
    if hidden_pages_ok and not broken:
        return
    if stack and not broken and pages <= limit:  # too big to walk here
        counted = _qpdf_page_count(content)
        pages = limit + 1 if counted is None else counted  # cannot tell: refuse
    if broken or pages > limit:
        raise ApiError(422, "damaged_pdf", DAMAGED_PDF_MESSAGE)


def _qpdf_page_count(content: bytes) -> int | None:
    """Pages qpdf finds following the page tree's kids; None when it cannot tell.

    In a child process: qpdf recurses down the tree, and one 40 000 levels deep
    overflowed its stack and killed this API worker (SIGBUS) mid-request.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "in.pdf"
        path.write_bytes(content)
        count = "import sys, pikepdf; print(len(pikepdf.open(sys.argv[1]).pages))"
        try:
            result = _spawn(
                [sys.executable, "-c", count, str(path)],
                timeout=REPAIR_WORKER_TIMEOUT,
                tmpdir=tmp,
                mem_bytes=GS_REPAIR_MEM_BYTES,
            )
        except subprocess.TimeoutExpired:
            return None
    return int(result.stdout) if result.returncode == 0 else None


def _pages_with_content(doc) -> int:
    """Pages whose page object and content streams survived MuPDF's repair, or
    -1 when any content reads back with a MuPDF warning: a file cut inside a
    page's content decodes short («premature end of data»), never with an error,
    and that page lost its last line behind a 200.
    """
    # ponytail: MuPDF's warning store is process-global, so this is exact while
    # one request runs at a time (containerConcurrency 1); a lock otherwise.
    pymupdf.TOOLS.mupdf_warnings()  # flush (a pending «repeated N times») and clear
    try:
        pages = sum(
            doc.xref_get_key(page.xref, "Type") == ("name", "/Page")
            and all(doc.xref_stream(x) is not None for x in page.get_contents())
            for page in doc
        )
    except Exception:
        return -1
    return -1 if pymupdf.TOOLS.mupdf_warnings() else pages


def _check_image_budget(doc, pages=None) -> None:
    """Refuse an embedded image too large to decode safely (see MAX_IMAGE_PIXELS)."""
    # Walk the pages, not range(page_count): that is the tree's /Count, which can
    # be wrong (6 over 4 real pages indexed page 5 and answered 500).
    for page in doc if pages is None else (doc[pno] for pno in pages):
        for img in page.get_images(full=True):
            width, height = img[2], img[3]
            if width * height > MAX_IMAGE_PIXELS:
                raise ApiError(
                    422,
                    "image_too_large",
                    "Este PDF contém uma imagem demasiado grande para processar em segurança.",
                )


def _resolve_convert_content_type(content_type: str | None, filename: str | None) -> str | None:
    if content_type:
        normalized = content_type.lower()
        if normalized in OFFICE_MIMES or normalized in IMAGE_MIMES:
            return normalized

    suffix = Path(filename or "").suffix.lower()
    return EXTENSION_TO_MIME.get(suffix)


# LibreOffice import filter per accepted Office type. Without --infilter,
# soffice content-sniffs and reaches every import filter it has: RTF bytes
# uploaded as "x.docx" converted, and so did DOC, ODT and HTML.
OFFICE_INFILTER = {
    ".docx": "MS Word 2007 XML",
    ".xlsx": "Calc MS Excel 2007 XML",
    ".pptx": "Impress MS PowerPoint 2007 XML",
}

# The main-part content type each OOXML package declares in [Content_Types].xml.
_OOXML_MAIN_TYPES = {
    b"wordprocessingml.document.main+xml": EXTENSION_TO_MIME[".docx"],
    b"spreadsheetml.sheet.main+xml": EXTENSION_TO_MIME[".xlsx"],
    b"presentationml.presentation.main+xml": EXTENSION_TO_MIME[".pptx"],
}

_FORMAT_LABEL = {
    EXTENSION_TO_MIME[".docx"]: "DOCX",
    EXTENSION_TO_MIME[".xlsx"]: "XLSX",
    EXTENSION_TO_MIME[".pptx"]: "PPTX",
    "image/jpeg": "JPG",
    "image/png": "PNG",
    "image/tiff": "TIFF",
}

UNSUPPORTED_FORMAT_MESSAGE = (
    "Formato não suportado. Pode converter ficheiros DOCX, XLSX, PPTX, JPG, PNG e TIFF."
)


def _sniff_convert_type(content: bytes) -> str | None:
    """Which of the six accepted formats the bytes really are, if any."""
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if content.startswith(b"PK\x03\x04"):
        import zipfile

        try:
            with (
                zipfile.ZipFile(io.BytesIO(content)) as package,
                package.open("[Content_Types].xml") as part,
            ):
                content_types = part.read(1024 * 1024)
        except (zipfile.BadZipFile, KeyError, OSError, RuntimeError):
            return None
        for marker, mime in _OOXML_MAIN_TYPES.items():
            if marker in content_types:
                return mime
    return None


_RGB_LIKE = ("DeviceRGB", "DeviceGray", "CalRGB", "CalGray")


def _images_to_rgb(doc) -> None:
    """Hand rewrite_images only gray and RGB images.

    MuPDF's lossy rewrite garbles ICC-based CMYK images — the normal encoding
    of print-ready PDFs — into grey stripes and a black band, with a 200.
    Converted to RGB first (colour-managed, soft mask kept) they are ordinary
    images it handles: rendered, the result matches the original to within
    1/255 on average.
    """
    done: set[int] = set()
    for page in doc:
        for img in page.get_images(full=True):
            xref, smask, cs_name = img[0], img[1], img[5]
            if xref in done or cs_name in _RGB_LIKE:
                continue
            done.add(xref)
            try:
                base = pymupdf.Pixmap(doc, xref)
                if base.colorspace is None or (
                    base.colorspace.n in (1, 3) and cs_name not in ("DeviceN", "Separation")
                ):
                    continue
                rgb = pymupdf.Pixmap(pymupdf.csRGB, base)
                if smask:
                    rgb = pymupdf.Pixmap(rgb, pymupdf.Pixmap(doc, smask))
                page.replace_image(xref, pixmap=rgb)
            except Exception:
                logger.warning("compress: could not convert image xref %s to RGB", xref)


def compress_pdf(content: bytes) -> bytes:
    """Compress a PDF using PyMuPDF.

    Every colour or grey image is re-encoded as JPEG q75 (lossy), and those
    above 150 dpi are also subsampled by a power of two that keeps them at
    96 dpi or more (300→150, 400→100; MuPDF never lands on 96 itself); 1-bit
    scans are left alone. Owner restrictions are kept. A result that is not
    smaller is refused (422 compress_no_gain) rather than sold.
    """
    doc = _open_pdf(content)
    try:
        _check_image_budget(doc)
        _images_to_rgb(doc)
        # bitonal=False: resampling a CCITT/JBIG2 scan to 150 ppi thinned the
        # strokes and cedillas for a few KB.
        doc.rewrite_images(dpi_threshold=150, dpi_target=96, quality=75, bitonal=False)
        result = doc.tobytes(
            garbage=4,
            deflate=True,
            clean=True,
            use_objstms=True,
            encryption=pymupdf.PDF_ENCRYPT_KEEP,
        )
    except ApiError:
        raise
    except Exception as exc:
        logger.warning("compress failed: %r", exc)
        raise ApiError(500, "compression_failed", "Não foi possível comprimir este PDF.") from exc
    finally:
        doc.close()

    if len(result) >= len(content):
        raise ApiError(
            422,
            "compress_no_gain",
            "Este PDF já está otimizado: não conseguimos reduzir mais o tamanho.",
        )
    return result


def flatten_pdf(content: bytes) -> bytes:
    """Flatten annotations and form fields into the page content."""
    doc = _open_pdf(content)
    try:
        doc.bake(annots=True, widgets=True)
        return doc.tobytes(garbage=4, deflate=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    except ApiError:
        raise
    except Exception as exc:
        logger.warning("flatten failed: %r", exc)
        raise ApiError(500, "flatten_failed", "Não foi possível achatar este PDF.") from exc
    finally:
        doc.close()


def _convert_office(content: bytes, content_type: str) -> bytes:
    ext = MIME_TO_EXT[content_type]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        input_path = Path(tmpdir) / f"input{ext}"
        output_path = Path(tmpdir) / "input.pdf"
        input_path.write_bytes(content)

        result = _run_command(
            [
                "soffice",
                "--headless",
                "--norestore",
                f"--infilter={OFFICE_INFILTER[ext]}",
                "--convert-to",
                "pdf",
                "--outdir",
                tmpdir,
                f"-env:UserInstallation=file://{tmpdir}/profile",
                str(input_path),
            ],
            timeout=TOOL_SUBPROCESS_TIMEOUT,
            tmpdir=tmpdir,
        )

        if result.returncode != 0 or not output_path.exists():
            stderr = _trim_process_output(result.stderr)
            logger.warning("soffice failed (rc=%s): %s", result.returncode, stderr)
            if "could not be loaded" in stderr:
                raise ApiError(
                    422,
                    "invalid_document",
                    f"Não foi possível abrir este {_FORMAT_LABEL[content_type]}. "
                    "Verifique se o ficheiro não está danificado.",
                )
            raise ApiError(500, "conversion_failed", "Falha na conversão do documento.")

        return output_path.read_bytes()


def _sanitize_image(content: bytes) -> bytes:
    """Re-encode an image to strip ICC profiles and problematic metadata.

    Some ICC profiles (e.g. short lcms "c2" profiles) cause Adobe Acrobat to
    render the resulting PDF as a solid black page.  Re-saving through Pillow
    without the original ICC profile eliminates the issue.

    The re-encode keeps what it used to lose: EXIF orientation (applied to the
    pixels — a portrait phone photo came out sideways), the DPI, every TIFF
    frame (a 3-page scan came out as page 1), and PNG/TIFF stay lossless.
    """
    from PIL import Image, ImageOps, ImageSequence

    img = Image.open(io.BytesIO(content))
    if not img.info.get("icc_profile"):
        return content  # nothing to strip; img2pdf reads EXIF orientation itself

    fmt = img.format
    dpi = img.info.get("dpi")
    if dpi and max(dpi) <= 1:
        dpi = None  # Pillow's stand-in for "no resolution"; img2pdf then uses its 96 dpi
    # MPO (phone JPEG) frames past the first are previews/depth maps, not pages.
    # exif_transpose returns a copy, taken while the iterator sits on that frame.
    source = [img] if fmt in ("JPEG", "MPO") else ImageSequence.Iterator(img)
    frames = [ImageOps.exif_transpose(frame) for frame in source]
    for frame in frames:
        frame.info.pop("icc_profile", None)
    save_kwargs: dict[str, object] = {"dpi": dpi} if dpi else {}

    out = io.BytesIO()
    if fmt in ("JPEG", "MPO"):
        frames[0].save(out, format="JPEG", quality=95, **save_kwargs)
    elif fmt == "TIFF":
        frames[0].save(
            out,
            format="TIFF",
            save_all=True,
            append_images=frames[1:],
            compression="group4" if frames[0].mode == "1" else "tiff_lzw",
            **save_kwargs,
        )
    else:
        frames[0].save(out, format="PNG", **save_kwargs)
    return out.getvalue()


def _reencode_frames(content: bytes) -> list[bytes]:
    """Last resort for an image img2pdf refuses: every frame as a plain RGB PNG."""
    from PIL import Image, ImageOps, ImageSequence

    img = Image.open(io.BytesIO(content))
    frames = [img] if img.format in ("JPEG", "MPO") else ImageSequence.Iterator(img)
    pages = []
    for frame in frames:
        frame = ImageOps.exif_transpose(frame)
        if frame.mode in ("RGBA", "LA", "P", "PA"):
            frame = frame.convert("RGBA")
            background = Image.new("RGB", frame.size, (255, 255, 255))
            background.paste(frame, mask=frame.getchannel("A"))
            frame = background
        out = io.BytesIO()
        frame.convert("RGB").save(out, format="PNG")
        pages.append(out.getvalue())
    return pages


def _image_to_pdf(content: bytes) -> bytes:
    from PIL import Image

    layout = img2pdf.get_layout_fun(
        pagesize=(img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297)),
        fit=img2pdf.FitMode.shrink,
        auto_orient=True,
    )
    try:
        try:
            return img2pdf.convert(_sanitize_image(content), layout_fun=layout)
        except Image.DecompressionBombError:
            raise
        except Exception:
            return img2pdf.convert(_reencode_frames(content), layout_fun=layout)
    except Image.DecompressionBombError as exc:
        raise ApiError(
            422, "image_too_large", "Esta imagem é demasiado grande para converter em segurança."
        ) from exc
    except Exception as exc:
        logger.warning("image conversion failed: %r", exc)
        raise ApiError(
            422,
            "invalid_image",
            "Não foi possível ler esta imagem. Verifique se o ficheiro não está danificado.",
        ) from exc


def convert_to_pdf(content: bytes, content_type: str | None, filename: str | None) -> bytes:
    """Convert a supported office document or image to PDF.

    The bytes decide the type (and so the LibreOffice import filter), among the
    six accepted formats only: a browser's generic MIME type (octet-stream,
    image/jpg) or a misnamed image still converts, while RTF/DOC/HTML dressed
    up as DOCX or PNG never reaches LibreOffice.
    """
    declared = _resolve_convert_content_type(content_type, filename)
    actual = _sniff_convert_type(content)
    if actual is None and declared in IMAGE_MIMES:
        # A WebP saved as .jpg (sites serve them under .jpg URLs) opens in every
        # viewer, and img2pdf reads it: convert it rather than call it damaged.
        from PIL import Image

        with suppress(Exception):
            if Image.open(io.BytesIO(content)).format in ("WEBP", "GIF", "BMP"):
                actual = declared
    if actual is None:
        if declared is None:
            raise ApiError(415, "unsupported_media_type", UNSUPPORTED_FORMAT_MESSAGE)
        raise ApiError(
            422,
            "invalid_document",
            f"Este ficheiro não é um {_FORMAT_LABEL[declared]} válido ou está danificado.",
        )

    if actual in OFFICE_MIMES:
        return _convert_office(content, actual)
    return _image_to_pdf(content)


def _render_dpi(page, dpi: int = 300) -> int:
    """Largest dpi ≤ `dpi` that keeps this page within MAX_RENDER_PIXELS."""
    area = max((page.rect.width / 72) * (page.rect.height / 72), 1e-6)
    return max(1, min(dpi, int((MAX_RENDER_PIXELS / area) ** 0.5)))


def _render_page(page, fmt: str) -> bytes:
    pix = page.get_pixmap(dpi=_render_dpi(page))
    if fmt == "jpeg":
        return pix.tobytes("jpeg", jpg_quality=92)
    return pix.tobytes("png")


def pdf_first_page_to_image(content: bytes, fmt: str) -> tuple[bytes, str, str]:
    """Render the first page of a PDF to PNG or JPEG (300 dpi, less for pages above A2)."""
    doc = _open_pdf(content)
    try:
        _check_image_budget(doc, pages=[0])
        image = _render_page(doc[0], fmt)
    except ApiError:
        raise
    except Exception as exc:
        logger.warning("pdf-to-image failed: %r", exc)
        raise ApiError(
            status_code=500,
            code="conversion_failed",
            message="Não foi possível converter este PDF para imagem.",
        ) from exc
    finally:
        doc.close()

    if len(image) > MAX_RESPONSE_BYTES:
        raise ApiError(
            422,
            "output_too_large",
            "A imagem desta página ultrapassa 30 MB, o máximo que conseguimos entregar."
            + (" Escolha o formato JPG, que ocupa menos." if fmt != "jpeg" else ""),
        )
    if fmt == "jpeg":
        return image, "image/jpeg", "jpg"
    return image, "image/png", "png"


MAX_PAGES_FOR_IMAGES = 20


def pdf_to_images(content: bytes, fmt: str) -> tuple[bytes, str, str]:
    """Render all pages of a PDF to images and return a ZIP archive."""
    import zipfile

    doc = _open_pdf(content)
    try:
        page_count = len(doc)
        if page_count > MAX_PAGES_FOR_IMAGES:
            raise ApiError(
                status_code=422,
                code="too_many_pages",
                message=(
                    f"O PDF tem {page_count} páginas (máximo: {MAX_PAGES_FOR_IMAGES}). "
                    "Use a ferramenta Extrair PDF para selecionar as páginas pretendidas."
                ),
            )
        _check_image_budget(doc)

        ext = "jpg" if fmt == "jpeg" else "png"
        digits = len(str(page_count))  # pagina-02 sorts before pagina-10
        deadline = time.monotonic() + PROCESSING_BUDGET_SECONDS
        buf = io.BytesIO()
        # Deflated, level 1: a document page is mostly white, and stored ZIPs of
        # its JPGs came out 37-175% bigger. Level 1 costs little on 20 pages.
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            for i, page in enumerate(doc):
                _check_deadline(deadline)
                zf.writestr(f"pagina-{i + 1:0{digits}d}.{ext}", _render_page(page, fmt))
                if buf.tell() > MAX_RESPONSE_BYTES:
                    raise ApiError(
                        422,
                        "output_too_large",
                        "As imagens de todas as páginas ultrapassam 30 MB, o máximo que "
                        "conseguimos entregar. "
                        + (
                            "Escolha o formato JPG, que ocupa menos, ou converta menos páginas."
                            if fmt != "jpeg"
                            else "Converta menos páginas de cada vez com a ferramenta "
                            "Extrair PDF."
                        ),
                    )

        return buf.getvalue(), "application/zip", "zip"
    except ApiError:
        raise
    except Exception as exc:
        logger.warning("pdf-to-images failed: %r", exc)
        raise ApiError(
            status_code=500,
            code="conversion_failed",
            message="Não foi possível converter este PDF para imagens.",
        ) from exc
    finally:
        doc.close()


def _pages_needing_ocr(doc) -> list[int]:
    """1-based pages worth OCR: mostly covered by images, or with almost no
    visible text (a scan, an old invisible OCR layer, outlined text).

    A scan with a small real-text footer («Digitalizado com …») counts: the old
    --skip-text skipped such a page whole and returned it unsearchable, 200.
    """
    pages = []
    for pno, page in enumerate(doc, start=1):
        area = abs(page.rect) or 1.0
        covered = sum(abs(pymupdf.Rect(info["bbox"]) & page.rect) for info in page.get_image_info())
        visible = sum(len(span["chars"]) for span in page.get_texttrace() if span["type"] != 3)
        if visible < 30 or covered / area >= 0.5:
            pages.append(pno)
    return pages


def _ocr_megapixels(page) -> float:
    """Megapixels OCRmyPDF will render for this page, colour counted twice.

    Mirrors ocrmypdf 17 (_pipeline.get_page_square_dpi, rasterize): the page
    renders at its images' highest resolution (at their area-weighted one when
    that is under 0.8 of it: a small signature on a scan), at 400 dpi or more
    when it has any text (invisible too) or vector painting, and in colour when
    an image is colour or anything is painted.
    test_ocr_megapixels_match_what_ocrmypdf_renders pins it against the PNG
    OCRmyPDF writes.
    """
    dpis, areas = [], []
    images = page.get_image_info()  # every image drawn, inline ones too
    for info in images:
        a, b, c, d = info["transform"][:4]
        shown_w, shown_h = math.hypot(a, b) / 72, math.hypot(c, d) / 72
        if info["width"] and info["height"] and shown_w and shown_h:
            dpis.append(max(info["width"] / shown_w, info["height"] / shown_h))
            areas.append(shown_w * shown_h)
    has_vector = bool(page.get_cdrawings())  # annotations count too: errs heavy
    dpi = max(dpis, default=0)
    if dpis:  # _pipeline.calculate_image_dpi: the area-weighted (harmonic) dpi
        weighted = sum(areas) / sum(a / d for a, d in zip(areas, dpis, strict=True))
        dpi = weighted if weighted < 0.8 * dpi else dpi
    if not dpis or has_vector or page.get_texttrace():
        dpi = max(dpi, 400)
    # By the PDF's own colour space, as OCRmyPDF reads it: a grey scan with an
    # ICC profile is rendered in colour. get_images lists no inline image:
    # those are read from the images drawn (an Indexed one counts 1 component).
    colour = (
        has_vector
        or any(
            bpc > 1 and cs not in ("DeviceGray", "CalGray", "Indexed")
            for _, _, _, _, bpc, cs, *_ in page.get_images(full=True)
        )
        or any(info["bpc"] > 1 and info["colorspace"] >= 3 for info in images)
    )
    inches = page.mediabox.width * page.mediabox.height / 72**2
    # 1-bit images only (a B/W scan) render mono: measured at 0.28 of a colour
    # pixel's time, so half a grey one. Read from the images drawn: get_images
    # lists no inline image, and a page with only a colour one priced as mono.
    mono = dpis and not has_vector and all(info["bpc"] == 1 for info in images)
    return inches * dpi**2 / 1e6 * (2 if colour else 0.5 if mono else 1)


def _busiest_ocr_worker(megapixels: list[float]) -> float:
    """Megapixels the busiest of OCR_JOBS workers renders. ocrmypdf 17 submits
    the pages in order to a process pool (builtin_plugins/concurrency.py), so
    each page goes to the first worker that frees up."""
    workers = [0.0] * OCR_JOBS
    for page in megapixels:
        heapq.heapreplace(workers, workers[0] + page)
    return max(workers)


def ocr_pdf(content: bytes, language: str) -> bytes:
    """Run OCRmyPDF with the requested language on the pages that need it."""
    lang_code = LANGUAGE_MAP.get(language)
    if lang_code is None:
        raise ApiError(
            status_code=400,
            code="unsupported_language",
            message=(
                f"Idioma não suportado: {language}. "
                f"Suportados: {', '.join(LANGUAGE_MAP.keys())}"
            ),
        )

    doc = _open_pdf(content)
    try:
        if doc.get_sigflags() > 0:  # ocrmypdf refuses these; say why instead of "corrompido"
            raise ApiError(
                422,
                "signed_pdf",
                "Este PDF tem uma assinatura digital, que o OCR iria invalidar. "
                "O OCR não está disponível para PDFs assinados.",
            )
        if doc.xref_get_key(doc.pdf_catalog(), "AcroForm/XFA")[0] != "null":
            raise ApiError(
                422, "xfa_form", "Este PDF é um formulário XFA, que o OCR não suporta."
            )
        pages = _pages_needing_ocr(doc)
        if not pages:
            raise ApiError(
                422,
                "already_searchable",
                "Este PDF já tem texto selecionável em todas as páginas: "
                "não há nada para reconhecer.",
            )
        form_pdf = bool(doc.is_form_pdf)
        if form_pdf and any(
            pymupdf.TextPage(doc[pno - 1].get_displaylist(annots=0).get_textpage())
            .extractText()
            .strip()
            for pno in pages
        ):
            raise ApiError(
                422,
                "form_needs_flattening",
                "Este PDF tem campos de formulário. Use «Achatar PDF» primeiro e "
                "depois volte a fazer OCR ao ficheiro achatado.",
            )
        if len(pages) > MAX_OCR_PAGES:
            raise ApiError(
                422,
                "too_many_pages",
                f"Este PDF tem {len(pages)} páginas para reconhecer e o OCR processa até "
                f"{MAX_OCR_PAGES} páginas de cada vez. Na ferramenta Dividir PDF, escolha "
                f"«A cada N páginas», escreva {MAX_OCR_PAGES} e processe cada parte.",
            )
        # --redo-ocr strips invisible text from the page's own content only, and
        # OCRmyPDF keeps its layer in a Form XObject (/OCR- + Name.random's 22
        # characters): OCR of our own output kept the old layer and added a second
        # copy of every word. Stripped before the budget, which priced that layer
        # (text renders at 400 dpi) and refused OCR of our own large results; and
        # only that name, or a user's /OCR-Logo went with it.
        stripped = False
        for pno in pages:
            for xref in doc[pno - 1].get_contents():
                old = doc.xref_stream(xref)
                new = _re.sub(rb"/OCR-[A-Za-z0-9_-]{22}\s+Do\b", b"", old)
                if new != old:
                    doc.update_stream(xref, new)
                    stripped = True
        megapixels = [_ocr_megapixels(doc[pno - 1]) for pno in pages]
        worker_budget = MAX_OCR_MEGAPIXELS / OCR_JOBS
        if max(megapixels) > worker_budget:
            raise ApiError(
                422,
                "page_too_large",
                "Este PDF tem páginas demasiado grandes ou com resolução demasiado alta "
                "para OCR.",
            )
        costs = [max(mpx, OCR_PAGE_FLOOR_MEGAPIXELS) for mpx in megapixels]
        if _busiest_ocr_worker(costs) > worker_budget:
            fit = sum(
                1
                for n in range(1, len(pages) + 1)
                if _busiest_ocr_worker(costs[:n]) <= worker_budget
            )
            raise ApiError(
                422,
                "too_many_pages",
                f"Este PDF tem {len(pages)} páginas para reconhecer, a cores ou em alta "
                f"resolução, e o OCR processa até {fit} páginas de cada vez. Na ferramenta Dividir "
                f"PDF, escolha «A cada N páginas», escreva {fit} e processe cada parte.",
            )
        _check_image_budget(doc, pages=[pno - 1 for pno in pages])
        all_pages = len(pages) == doc.page_count
        mode = "--skip-text" if form_pdf else "--redo-ocr"
        if stripped:
            content = doc.tobytes()
    finally:
        doc.close()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        input_path = Path(tmpdir) / "input.pdf"
        output_path = Path(tmpdir) / "output.pdf"
        input_path.write_bytes(content)

        result = _run_command(
            [
                "ocrmypdf",
                mode,
                *OCR_FLAGS,
                *OCR_DOWNSAMPLE_FLAGS,
                *OCR_TESSERACT_TIMEOUT_FLAGS,
                "-l",
                lang_code,
                *([] if all_pages else ["--pages", ",".join(map(str, pages))]),
                str(input_path),
                str(output_path),
            ],
            timeout=OCR_SUBPROCESS_TIMEOUT,
            tmpdir=tmpdir,
        )

        if result.returncode != 0 or not output_path.exists():
            logger.warning(
                "ocrmypdf failed (rc=%s): %s",
                result.returncode,
                _trim_process_output(result.stderr),
            )
            if result.returncode == 2:  # input error the checks above did not predict
                raise ApiError(400, "invalid_pdf", INVALID_PDF_MESSAGE)
            raise ApiError(
                status_code=500,
                code="ocr_failed",
                message="Falha no processamento OCR.",
            )

        output = output_path.read_bytes()
        with pymupdf.open(stream=output, filetype="pdf") as result_doc:
            found_text = False
            for pno in pages:
                page = result_doc[pno - 1]
                if page.get_text().strip() and any(
                    span["type"] == 3 and span["chars"] for span in page.get_texttrace()
                ):
                    found_text = True
                    break
        if not found_text:
            raise ApiError(
                422,
                "ocr_no_text",
                "O OCR não encontrou texto para reconhecer nas imagens deste PDF.",
            )
        return output


def convert_pdf_to_pdfa(content: bytes, conformance: str) -> bytes:
    """Convert a PDF into the requested PDF/A conformance."""
    pdfa_level = CONFORMANCE_MAP.get(conformance)
    if pdfa_level is None:
        raise ApiError(
            status_code=400,
            code="invalid_conformance",
            message=(
                f"Nível de conformidade inválido: {conformance}. "
                f"Suportados: {', '.join(CONFORMANCE_MAP.keys())}"
            ),
        )

    pdfa_definition_template = next(Path("/usr/share/ghostscript").rglob("PDFA_def.ps"), None)
    icc_profile_path = Path("/usr/share/color/icc/ghostscript/default_rgb.icc")
    if pdfa_definition_template is None or not icc_profile_path.exists():
        logger.error("Ghostscript PDF/A resources missing (PDFA_def.ps or default_rgb.icc)")
        raise ApiError(
            status_code=503,
            code="tool_unavailable",
            message=TOOL_UNAVAILABLE_MESSAGE,
        )

    # Validate first: Ghostscript 10 exits 0 with one blank page for a
    # password-protected or empty upload, and runs a %!PS upload as a program.
    # Ghostscript converts every page the /Kids hold, whatever MuPDF counts (P2-2).
    doc = _open_pdf(content, hidden_pages_ok=True)
    try:
        text_chars = [len(page.get_text().strip()) for page in doc]
        # The real pages: qpdf follows the page tree's kids, as Ghostscript does.
        # MuPDF's page_count is the tree's /Count, which can be wrong either way.
        page_count = _qpdf_page_count(content) or len(text_chars)
        metadata = doc.metadata or {}
        source = _prepare_for_pdfa(doc, pdfa_level)
    finally:
        doc.close()
    if source is None:
        source = content[_pdf_start(content) :]

    # The stock template stamps /Title (Title) over the document's own title.
    # The real title goes back in after gs (see _finish_pdfa).
    template_text = _re.sub(
        r"\[\s*/Title \(Title\)[^\n]*\n\s*/DOCINFO pdfmark",
        "",
        pdfa_definition_template.read_text(encoding="latin-1"),
    )

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_path = tmpdir_path / "input.pdf"
        output_path = tmpdir_path / "output.pdf"
        pdfa_definition_path = tmpdir_path / "PDFA_def.ps"

        input_path.write_bytes(source)
        pdfa_definition_path.write_text(
            template_text.replace("/ICCProfile (srgb.icc)", f"/ICCProfile ({icc_profile_path})"),
            encoding="latin-1",
        )

        result = _run_command(
            [
                "gs",
                "-dSAFER",
                f"-dPDFA={pdfa_level}",
                "-dBATCH",
                "-dNOPAUSE",
                "-sDEVICE=pdfwrite",
                "-sColorConversionStrategy=RGB",
                "-dPDFACompatibilityPolicy=1",
                f"--permit-file-read={icc_profile_path}:{pdfa_definition_path}:{input_path}",
                f"-sOutputFile={output_path}",
                str(pdfa_definition_path),
                str(input_path),
            ],
            timeout=TOOL_SUBPROCESS_TIMEOUT,
            tmpdir=tmpdir,
        )

        if result.returncode != 0 or not output_path.exists():
            logger.warning(
                "gs pdfa failed (rc=%s): %s",
                result.returncode,
                _trim_process_output(result.stderr),
            )
            raise ApiError(
                status_code=500,
                code="pdfa_conversion_failed",
                message="Falha na conversão para PDF/A.",
            )
        output = output_path.read_bytes()

    _check_pdfa_output(output, page_count, text_chars, pdfa_level)
    return _finish_pdfa(output, metadata, pdfa_level)


def _prepare_for_pdfa(doc, pdfa_level: str) -> bytes | None:
    """Fix what Ghostscript would otherwise get wrong; None when nothing to fix.

    Links: gs drops annotations without the Print flag in PDF/A mode, so every
    mailto/URL link vanished. Attachments: PDF/A-1 forbids them and PDF/A-2
    allows only PDF/A files, yet gs kept them and the XMP still claimed
    conformance — refuse instead and point to PDF/A-3b, which keeps them.
    """
    if pdfa_level in ("1", "2"):
        has_attachment_annots = any(
            annot.type[0] == pymupdf.PDF_ANNOT_FILE_ATTACHMENT
            for page in doc
            for annot in page.annots()
        )
        if doc.embfile_count() or has_attachment_annots:
            raise ApiError(
                422,
                "pdfa_attachments",
                f"Este PDF tem ficheiros anexados, que o PDF/A-{pdfa_level}b não permite. "
                "Escolha PDF/A-3b, que os mantém.",
            )
    changed = False
    for page in doc:
        for link in page.get_links():
            xref = link.get("xref")
            if xref and doc.xref_get_key(xref, "F") != ("int", "4"):
                doc.xref_set_key(xref, "F", "4")  # Print on; Hidden/NoView off
                changed = True
    return doc.tobytes() if changed else None


def _check_pdfa_output(output: bytes, page_count: int, text_chars: list[int], level: str) -> None:
    try:
        out = pymupdf.open(stream=output, filetype="pdf")
    except Exception as exc:
        raise ApiError(500, "pdfa_conversion_failed", "Falha na conversão para PDF/A.") from exc
    try:
        if out.page_count != page_count:
            logger.warning("gs pdfa lost pages: %s of %s", out.page_count, page_count)
            raise ApiError(
                422,
                "pdfa_conversion_failed",
                "Não foi possível converter todas as páginas deste PDF para PDF/A.",
            )
        if level == "1":
            for before, page in zip(text_chars, out, strict=False):  # /Count may be wrong
                # PDF/A-1 has no transparency: gs turns such a page into one
                # picture, so its text can no longer be searched or copied.
                if before >= 20 and len(page.get_text().strip()) < before // 10:
                    raise ApiError(
                        422,
                        "pdfa1_transparency",
                        "Este PDF tem transparências, que o PDF/A-1b não permite: o texto "
                        "dessas páginas deixaria de ser pesquisável. Escolha PDF/A-2b, "
                        "que mantém o texto.",
                    )
    finally:
        out.close()


def _finish_pdfa(output: bytes, metadata: dict, level: str) -> bytes:
    """Put back what Ghostscript 10.0 leaves out of the PDF/A file.

    Metadata: gs discards the whole DOCINFO when one string is not plain ASCII
    ("cannot be represented in XMP") — «Relatório» was enough to lose the
    title and the author. pikepdf writes XMP and Info together, as PDF/A asks.

    Attachments (PDF/A-3): each one must say how it relates to the document
    (/AFRelationship), carry a MIME /Subtype, and be listed in the catalog's
    /AF. gs copies the files but writes none of that.
    """
    import pikepdf

    fields = {
        xmp_key: (value if xmp_key != "dc:creator" else [value])
        for xmp_key, key in (
            ("dc:title", "title"),
            ("dc:creator", "author"),
            ("dc:description", "subject"),
            ("pdf:Keywords", "keywords"),
        )
        if (value := (metadata.get(key) or "").strip())
    }
    with pikepdf.open(io.BytesIO(output)) as pdf:
        names = pdf.Root.get("/Names")
        attachments = level == "3" and names is not None and "/EmbeddedFiles" in names
        if not fields and not attachments:
            return output
        if fields:
            with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                for key, value in fields.items():
                    meta[key] = value
        if attachments:
            tree = pikepdf.NameTree(names.EmbeddedFiles)
            specs = []
            for name in list(tree.keys()):
                spec = tree[name]
                if not spec.is_indirect:  # /AF and the name tree must share one object
                    spec = pdf.make_indirect(spec)
                    tree[name] = spec
                spec.AFRelationship = pikepdf.Name.Unspecified
                embedded = spec.get("/EF", {}).get("/F")
                if embedded is not None and "/Subtype" not in embedded:
                    embedded.Subtype = pikepdf.Name("/application/octet-stream")
                specs.append(spec)
            if specs:
                pdf.Root.AF = pikepdf.Array(specs)
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()


def _pikepdf_open(content: bytes, **kwargs):
    """pikepdf.open, after refusing a page tree deeper than MAX_PAGE_TREE_DEPTH
    (see there): qpdf walks it on open, to push inherited attributes down."""
    import pikepdf

    with pikepdf.open(io.BytesIO(content), inherit_page_attributes=False, **kwargs) as pdf:
        # One level at a time, each node once, so a cycle ends.
        level, seen = [pdf.Root.get("/Pages")], set()
        for _ in range(MAX_PAGE_TREE_DEPTH):
            kids = []
            for node in level:
                if not isinstance(node, pikepdf.Dictionary) or node.objgen in seen:
                    continue
                if node.is_indirect:
                    seen.add(node.objgen)
                if isinstance(children := node.get("/Kids"), pikepdf.Array):
                    kids.extend(children)
            if not kids:
                break
            level = kids
        else:
            raise ApiError(422, "damaged_pdf", DAMAGED_PDF_MESSAGE)
    return pikepdf.open(io.BytesIO(content), **kwargs)


def _open_pikepdf(content: bytes):
    import pikepdf

    _pdf_start(content)
    try:
        pdf = _pikepdf_open(content)
    except ApiError:
        raise
    except pikepdf.PasswordError as exc:
        raise ApiError(
            status_code=400,
            code="password_protected_pdf",
            message="Este PDF já está protegido com palavra-passe.",
        ) from exc
    except Exception as exc:
        raise ApiError(
            status_code=400,
            code="invalid_pdf",
            message=INVALID_PDF_MESSAGE,
        ) from exc
    # qpdf rebuilds a truncated file quietly: 30 of 60 pages, saved as if whole,
    # or every page with the last one's content cut short.
    try:
        with pymupdf.open(stream=content, filetype="pdf") as doc:
            damaged = doc.is_repaired and (
                doc.page_count != len(pdf.pages) or _pages_with_content(doc) != doc.page_count
            )
    except Exception:
        damaged = False
    if damaged or len(pdf.pages) == 0:
        pdf.close()
        raise ApiError(422, "damaged_pdf", DAMAGED_PDF_MESSAGE)
    return pdf


# PDF 2.0 / AES-256 (R6) passwords are at most 127 UTF-8 bytes: every reader
# (qpdf, poppler, MuPDF, pdf.js) truncates there, so a longer one locked the
# file for good — not even its first 127 characters opened it.
MAX_PASSWORD_BYTES = 127


def protect_pdf(content: bytes, password: str) -> bytes:
    """Encrypt a PDF with AES-256 permissions."""
    import pikepdf

    if not password or not password.strip():
        raise ApiError(
            status_code=400,
            code="invalid_password",
            message="A palavra-passe é obrigatória.",
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ApiError(
            status_code=400,
            code="password_too_long",
            message=(
                f"A palavra-passe é demasiado longa. Use no máximo {MAX_PASSWORD_BYTES} "
                "caracteres (letras acentuadas e símbolos como € contam a dobrar ou mais)."
            ),
        )

    pdf = _open_pikepdf(content)
    permissions = pikepdf.Permissions(
        accessibility=True,
        extract=False,
        modify_annotation=False,
        modify_assembly=False,
        modify_form=False,
        modify_other=False,
        print_lowres=True,
        print_highres=True,
    )

    try:
        buf = io.BytesIO()
        pdf.save(
            buf,
            encryption=pikepdf.Encryption(
                owner=password,
                user=password,
                R=6,
                aes=True,
                allow=permissions,
            ),
        )
        return buf.getvalue()
    except Exception as exc:
        raise ApiError(
            status_code=500,
            code="protection_failed",
            message="Não foi possível proteger o PDF.",
        ) from exc
    finally:
        pdf.close()


def _unsupported_encryption(exc: Exception) -> ApiError:
    return ApiError(
        status_code=422,
        code="unsupported_encryption",
        message=(
            "Não foi possível abrir este PDF. A cifra pode não ser suportada "
            "(ex. certificado) ou o ficheiro está corrompido."
        ),
    )


def _open_for_unlock(content: bytes, password: str):
    """Open an encrypted PDF for decryption: try no password first (owner-only
    PDFs have an empty user password), then the supplied one.

    Every open failure — on EITHER attempt — maps to a typed ApiError so a
    corrupt/unsupported file never leaks a 500. A PdfError on the retry open
    must be caught here: a sibling ``except`` clause does not catch exceptions
    raised inside another handler.
    """
    import pikepdf

    _pdf_start(content)  # a PNG was told «A cifra pode não ser suportada»
    try:
        return _pikepdf_open(content)  # no password first
    except pikepdf.PasswordError:
        pass  # needs a real open password — fall through
    except pikepdf.PdfError as exc:
        raise _unsupported_encryption(exc) from exc

    if not password:
        raise ApiError(
            status_code=400,
            code="password_required",
            message="Este PDF precisa de uma palavra-passe para abrir.",
        )
    try:
        return _pikepdf_open(content, password=password)
    except pikepdf.PasswordError as exc:
        raise ApiError(
            status_code=400,
            code="wrong_password",
            message="Palavra-passe incorreta.",
        ) from exc
    except pikepdf.PdfError as exc:
        raise _unsupported_encryption(exc) from exc


def unlock_pdf(content: bytes, password: str = "") -> bytes:
    """Remove password/permission encryption from a PDF.

    The only tool whose input is legitimately encrypted. Two cases:
      * owner-restriction-only PDF (empty user password) -> opens with NO
        password; saving without encryption strips the restrictions.
      * user (open) password PDF -> needs the supplied password to open.
    pikepdf never raises when a password is passed to a non-encrypted PDF, so
    the not_encrypted case is detected with an explicit is_encrypted check.
    """
    pdf = _open_for_unlock(content, password)
    try:
        if not pdf.is_encrypted:
            raise ApiError(
                status_code=422,
                code="not_encrypted",
                message="Este PDF não está protegido — não há nada a desbloquear.",
            )
        if len(pdf.pages) > MAX_PAGES:
            raise ApiError(
                status_code=422,
                code="too_many_pages",
                message=f"O PDF tem demasiadas páginas (máximo: {MAX_PAGES}).",
            )
        buf = io.BytesIO()
        pdf.save(buf, encryption=False)  # explicit: strip all encryption/permissions
        return buf.getvalue()
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(
            status_code=500,
            code="unlock_failed",
            message="Não foi possível desbloquear o PDF.",
        ) from exc
    finally:
        pdf.close()


def _set_field_value(field: Any, value: Any) -> None:
    import pikepdf
    from pikepdf.form import (
        CheckboxField,
        ChoiceField,
        MultipleFieldProxy,
        RadioButtonGroup,
        TextField,
    )

    if isinstance(field, MultipleFieldProxy):
        for sub_field in field:
            _set_field_value(sub_field, value)
        return

    if isinstance(field, CheckboxField):
        field.checked = bool(value)
    elif isinstance(field, RadioButtonGroup):
        str_value = str(value)
        for opt in field.options:
            if str(opt.on_value) == str_value or str(opt.on_value) == f"/{str_value}":
                opt.select()
                return
        field.value = pikepdf.Name(f"/{str_value}")
    elif isinstance(field, (ChoiceField, TextField)):
        field.value = str(value)
    else:
        field.value = str(value)


def fill_form_pdf(
    content: bytes,
    field_values: dict[str, Any],
    *,
    strict_unknown_fields: bool,
) -> bytes:
    """Fill an AcroForm PDF and flatten the result."""
    from pikepdf.form import ExtendedAppearanceStreamGenerator, Form

    if not isinstance(field_values, dict) or not field_values:
        raise ApiError(
            status_code=400,
            code="missing_form_values",
            message="Nenhum valor de campo fornecido.",
        )

    pdf = _open_pikepdf(content)
    if pdf.Root.get("/AcroForm") is None:
        pdf.close()
        raise ApiError(
            status_code=400,
            code="missing_form_fields",
            message="Este PDF não contém campos de formulário.",
        )

    try:
        form = Form(pdf, generate_appearances=ExtendedAppearanceStreamGenerator)
        unknown_fields: list[str] = []

        for field_name, value in field_values.items():
            try:
                field = form[field_name]
            except KeyError:
                unknown_fields.append(field_name)
                continue
            _set_field_value(field, value)

        if strict_unknown_fields and unknown_fields:
            raise ApiError(
                status_code=422,
                code="unknown_form_fields",
                message="Alguns nomes de campo não existem no formulário PDF carregado.",
                details={"unknownFields": sorted(unknown_fields)},
            )

        pdf.flatten_annotations("all")
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(
            status_code=500,
            code="form_processing_failed",
            message="Não foi possível processar o formulário.",
        ) from exc
    finally:
        pdf.close()


def _image_box(page, item) -> tuple[float, ...] | None:
    try:
        return tuple(round(v, 1) for v in page.get_image_bbox(item))
    except Exception:  # drawn through a form XObject, or not drawn at all
        return None


def _jpeg_image_boxes(page) -> set[tuple[float, ...]]:
    """Where the page draws a JPEG, recorded before a redaction rewrites it."""
    boxes = {_image_box(page, i) for i in page.get_images(full=True) if i[8] == "DCTDecode"}
    boxes.discard(None)
    return boxes


def _store_redacted_jpegs_as_jpeg(page, jpeg_boxes: set[tuple[float, ...]]) -> None:
    """Blanking pixels makes MuPDF rewrite the image unfiltered, and the save
    stores it lossless: a photo page came back up to 8x the input, past what
    Cloud Run delivers. A JPEG before stays a JPEG after. Gray and RGB only —
    a CMYK JPEG's inversion is not portable across readers; it stays lossless.
    """
    doc = page.parent
    for item in page.get_images(full=True):
        xref, image_filter = item[0], item[8]
        if image_filter or _image_box(page, item) not in jpeg_boxes:
            continue
        pix = pymupdf.Pixmap(doc, xref)
        if pix.alpha or pix.n not in (1, 3):
            continue
        doc.update_stream(xref, pix.tobytes("jpeg", jpg_quality=88), compress=False)
        doc.xref_set_key(xref, "Filter", "/DCTDecode")


def _image_placements(page, among: set[int]) -> dict[int, tuple]:
    """Every box where the page draws each of these image objects, and its size.
    get_images lists an image once per resource name, and one name can be drawn
    twice. No box: listed, never drawn — pages sharing one resource dictionary
    each list every page's images."""
    placements: dict[int, tuple] = {}
    for item in page.get_images(full=True):
        if item[0] in among:
            boxes = placements.setdefault(item[0], (set(), item[2], item[3]))[0]
            with suppress(Exception):  # drawn through a form XObject
                for rect in page.get_image_rects(item):
                    if rect.x0 < rect.x1 and rect.y0 < rect.y1:  # not (1, 1, -1, -1)
                        boxes.add(tuple(round(v, 1) for v in rect))
    return placements


def _apply_redactions(doc, page_numbers, deadline: float, *, marks: bool = False) -> None:
    """apply_redactions on these pages, and what it blanks in a scan blanked
    wherever the scan is drawn.

    MuPDF blanks a copy of an image for the place it redacts; any other place
    drawing the same image — another page, or this one again — kept the
    original: the email intact under a crop or a white box. Each redaction is
    copied back over the original, so the next page redacts from there, and in
    the end every copy holds every blanked box.

    Only under a box _in_pixels allows — an OCR layer over a scan, or another
    editor's mark over no visible text. The page's other boxes wait until every
    scan is blanked everywhere, then blank this page's copy alone: visible text
    over a shared scan or background is not in its pixels, and blanked
    everywhere, every other page got a hole where the email was. A mark over
    visible text and a shared image is refused: the image may hold what it
    marks, or be a background.

    ponytail: every shared image under an OCR box is blanked everywhere, a
    background under a pasted OCR'd scan too (a hole on other pages). Which
    image shows there takes a render to tell: a clip let a scan show through an
    opaque photo whose box covered the email.
    """
    blanked: dict[int, list[int]] = {}
    held_back: list[tuple[int, list]] = []
    pages_per_image = Counter(
        xref for page in doc for xref in {i[0] for i in page.get_images(full=True)}
    )
    pages_per_image.update(_drawn_in_a_layer_off(doc, page_numbers))
    for page_idx in page_numbers:
        _check_deadline(deadline)
        page = doc[page_idx]
        drawn = {item[0] for item in page.get_images(full=True)}
        marked = list(page.annots(types=(pymupdf.PDF_ANNOT_REDACT,)))
        in_pixels = _in_pixels(page.get_texttrace(), marks=marks)
        before = _image_placements(page, drawn) if marked and drawn else {}
        local = [annot for annot in marked if not in_pixels(annot.rect)]
        if marks and local and drawn and _mark_over_shared_image(
            doc, page_idx, [annot.rect for annot in local], pages_per_image
        ):
            raise ApiError(
                422,
                "mark_over_shared_image",
                "Este PDF tem uma marca de censura por aplicar sobre uma imagem que se repete "
                "noutras páginas, e não a conseguimos censurar em todas com segurança. "
                + _REDACT_AS_IMAGES,
            )
        boxes = [annot.rect for annot in marked if in_pixels(annot.rect)]
        shared = _shared_under(before, boxes, pages_per_image)
        later = local if shared else []
        if later:
            held_back.append((page_idx, [(annot.rect, _redact_keys(annot)) for annot in later]))
            for annot in later:
                page.delete_annot(annot)
        _redact_page(page)
        if not shared:
            continue
        page = doc.reload_page(page)  # get_image_rects caches where images were
        now = {item[0] for item in page.get_images(full=True)}
        after = _image_placements(page, now - drawn)
        # Only what the page drew: the cleanup after a redaction also drops
        # images it merely listed, drawn elsewhere.
        gone = {x for x, place in before.items() if place[0]} - now
        pairs = _redacted_copies(before, after, gone)
        # Drawn twice, redacted twice: two copies, each blank only in its own
        # box; one over the image undid the other. The preview refuses it
        # where the image shows anywhere else; under a mark, it goes.
        once = {x for x, n in Counter(x for x, _ in pairs).items() if n == 1}
        for original, copy in pairs:
            if original in shared & once:
                _copy_object(doc, copy, original)
                blanked.setdefault(original, []).append(copy)
        for original in shared & (gone | {x for x, _ in pairs}) - once:
            page.replace_image(original, pixmap=_NO_IMAGE)
    for original, copies in blanked.items():
        for copy in copies:
            _copy_object(doc, original, copy)
    page = marked = later = None  # reload_page refuses a page something else holds
    for page_idx, kept in held_back:
        _check_deadline(deadline)
        page = doc.reload_page(doc[page_idx])  # get_image_info caches where images were
        for rect, keys in kept:
            _add_redaction(page, rect, keys)
        _redact_page(page)


def _redact_page(page) -> None:
    jpeg_boxes = _jpeg_image_boxes(page)
    _fill_unstroked(page)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_PIXELS,  # blank what the box covers
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,  # explicit: delete characters
    )
    _store_redacted_jpegs_as_jpeg(page, jpeg_boxes)


def _fill_unstroked(page) -> None:
    """Paint the boxes over no image here, without the stroke PyMuPDF adds.

    apply_redactions fills each box and strokes it in the fill colour, 1 pt
    wide: half a point past every edge, a black rule under «5 de Outubro» on
    the line above a redacted phone in a real footer. Over an image the stroke
    stays: it covers the edge of what the box blanks. Painted before
    apply_redactions, under any overlay text PyMuPDF writes after it.
    """
    images = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
    shape = None
    for annot in page.annots(types=(pymupdf.PDF_ANNOT_REDACT,)):
        fill = annot.colors["fill"]
        if not fill or any(annot.rect.intersects(image) for image in images):
            continue
        if shape is None:
            shape = page.new_shape()
        shape.draw_rect(annot.rect)
        shape.finish(fill=fill, color=None, width=0)
        page.parent.xref_set_key(annot.xref, "IC", "null")
    if shape is not None:
        shape.commit()


def _drawn_in_a_layer_off(doc, page_numbers) -> set[int]:
    """Images these pages draw again in a layer that is off. The page's own
    placements miss them, so the image was not taken for drawn twice, and that
    placement, off the box, kept the scan as it was: switched on, it showed the
    email. Counted as shared, the image is blanked everywhere."""
    seen = _with_every_layer_on(doc)
    if seen is doc:
        return set()
    try:
        again = set()
        for number in page_numbers:
            listed = {item[0] for item in doc[number].get_images(full=True)}
            if listed:
                shown = _image_placements(doc[number], listed)
                every = _image_placements(seen[number], listed)
                again |= {x for x in listed if len(every[x][0]) > len(shown[x][0])}
        return again
    finally:
        seen.close()


def _mark_over_shared_image(doc, page_number: int, boxes: list, pages_per_image) -> bool:
    """Whether these boxes cover an image the page draws twice, or another page
    draws too. Looked at with every layer on: a layer that is off draws
    nothing, yet any reader switches it on — and there the scan kept what the
    mark covered."""
    seen = _with_every_layer_on(doc)
    try:
        page = seen[page_number]
        placements = _image_placements(page, {i[0] for i in page.get_images(full=True)})
        return any(
            len(placements[x][0]) > 1 or _drawn_elsewhere(seen, x, page_number)
            for x in _shared_under(placements, boxes, pages_per_image)
        )
    finally:
        if seen is not doc:
            seen.close()


def _drawn_elsewhere(doc, xref: int, page_number: int) -> bool:
    """Whether another page draws the image, not merely lists it: pages sharing
    one resource dictionary each list every page's images."""
    return any(
        _image_placements(page, {xref}).get(xref, (set(),))[0]
        for page in doc
        if page.number != page_number and xref in {i[0] for i in page.get_images(full=True)}
    )


def _shared_under(placements: dict, boxes: list, pages_per_image: Counter) -> set[int]:
    """The images under these boxes that another page or place draws too."""
    return {
        x for x, (places, *_) in placements.items()
        if (pages_per_image[x] > 1 or len(places) > 1)
        and any(pymupdf.Rect(place).intersects(box) for place in places for box in boxes)
    }


def _check_image_copies(page, boxes: list, pages_per_image: Counter) -> None:
    """Refuse what _apply_redactions cannot put back without hiding more: an
    image these boxes cover in two places that shows anywhere else, or two
    images they cover alike in place and size, one of them shown elsewhere.
    Nothing tells those blanked copies apart, and the image went from every
    page — a logo the email was never in vanished from the next one.

    get_image_rects finds an image by its pixels, so two objects with the same
    pixels share their places; alike, they are not told apart, nor need to be.
    """
    sizes = {item[0]: item[2:4] for item in page.get_images(full=True)}
    placements = _image_placements(page, set(sizes)) if sizes else {}
    alike: dict[tuple, set[int]] = {}
    for xref, (places, *size) in placements.items():
        hit = {place for place in places if any(pymupdf.Rect(place).intersects(b) for b in boxes)}
        if len(hit) > 1 and (pages_per_image[xref] > 1 or len(places) > len(hit)):
            raise ApiError(
                422,
                "image_redacted_twice",
                "Este PDF desenha a mesma imagem em vários sítios, e não a conseguimos "
                "censurar em todos sem a apagar noutras páginas. " + _REDACT_AS_IMAGES,
            )
        for place in hit:
            alike.setdefault((place, *size), set()).add(xref)
    for xrefs in alike.values():
        if (
            len(xrefs) > 1
            and any(pages_per_image[x] > 1 or len(placements[x][0]) > 1 for x in xrefs)
            and len({pymupdf.Pixmap(page.parent, x).digest for x in xrefs}) > 1
        ):
            raise ApiError(
                422,
                "images_alike",
                "Este PDF tem imagens sobrepostas, do mesmo tamanho, que se repetem noutras "
                "páginas, e não conseguimos censurar só a certa. " + _REDACT_AS_IMAGES,
            )


def _redacted_copies(before: dict, after: dict, gone: set[int]) -> list[tuple[int, int]]:
    """Pair each image a redaction put on the page with the one it replaced.

    By a box it is drawn in and its size — the redaction renames the resource
    (fzImg0 became Im1), and an image drawn twice keeps its other place — or,
    where boxes are alike (a photo under a full-page overlay of its size) or
    the copy's cannot be read, by size when one image of it left the page and
    one copy of it came: a logo still on the page is never a candidate.
    Pixels cannot decide: a blanked copy looked more like the logo than its
    own photo. A wrong pair put one image over another.
    """
    def keys(place) -> set:
        boxes, *size = place
        return {(box, *size) for box in boxes}

    alike = Counter(key for place in before.values() for key in keys(place))
    at = {key: x for x, place in before.items() for key in keys(place) if alike[key] == 1}
    pairs = []
    for copy, place in after.items():
        if len(found := {at[key] for key in keys(place) if key in at}) == 1:
            pairs.append((found.pop(), copy))
    paired = {original for original, _ in pairs} | {copy for _, copy in pairs}
    left = Counter(before[x][1:] for x in gone - paired)
    came = Counter(place[1:] for copy, place in after.items() if copy not in paired)
    for copy, place in after.items():
        if copy not in paired and left[place[1:]] == came[place[1:]] == 1:
            pairs += [(x, copy) for x in gone - paired if before[x][1:] == place[1:]]
    return pairs


# What MuPDF leaves of an image wholly inside a redaction box: nothing, and every
# other page drawing it showed the whole image. A shared image that left the page
# with no copy to pair — removed, or alike another — is drawn by no page: more
# than was asked, never less.
_NO_IMAGE = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 1, 1), True)
_NO_IMAGE.clear_with()  # transparent: with a value, alpha stays opaque


def _copy_object(doc, source: int, target: int) -> None:
    doc.update_stream(target, doc.xref_stream_raw(source), compress=False)
    doc.update_object(target, doc.xref_object(source, compressed=True))


# Replacement text: what a reader copies or reads aloud instead of the glyphs, so
# a copy of the page text. MuPDF edits /Alt glyph by glyph as it redacts, but an
# edited /ActualText it writes into /Alt and leaves the original (1.27
# pdf-op-filter.c, update_mcid): the email came back in text extraction.
# Without it, a screen reader reads the drawn glyphs — the redacted text.
_REPLACEMENT_TEXT = ("/ActualText", "/E")


def _content_unreadable() -> ApiError:
    return ApiError(
        422,
        "content_unreadable",
        "Este PDF tem conteúdo danificado, onde não conseguimos garantir que "
        "o texto censurado sai de todas as cópias. " + _REDACT_AS_IMAGES,
    )


# pikepdf holds ~55 bytes per byte of marked content it parses: 8 MiB cost 441 MiB.
_MAX_PARSED_CONTENT_BYTES = 8 * 1024 * 1024


def _inflate(raw: bytes) -> Iterator[bytes]:
    """A Flate stream in 1 MiB pieces; a cut one ends where it stops, a broken
    one is refused. Its checksum is not read, as MuPDF and qpdf do not: a wrong
    one ended the scan, and the stream's /ActualText kept the email."""
    inflate = zlib.decompressobj(-zlib.MAX_WBITS)  # raw deflate: no checksum
    raw = raw[2:]  # the zlib header
    try:
        while raw and not inflate.eof:
            piece = inflate.decompress(raw, 1 << 20)
            if not piece and inflate.unconsumed_tail == raw:
                break  # no progress
            raw = inflate.unconsumed_tail
            yield piece
        yield inflate.flush()
    except zlib.error as exc:
        raise _content_unreadable() from exc


def _scan_content(parts, deadline: float) -> tuple[int, bool]:
    """A content stream's decoded size, and whether a BDC or DP in it may carry
    an inline dictionary. Read in pieces, never whole: a 260 KB file held an
    unused 256 MiB form, and reading it at once cost 386 MiB."""
    import pikepdf

    size, seen, tail = 0, set(), b""
    for part in parts:
        if not isinstance(part, pikepdf.Stream):
            continue
        filters = part.get("/Filter")
        filters = list(filters) if isinstance(filters, pikepdf.Array) else [filters]
        try:
            if filters == [None]:
                pieces = [part.read_raw_bytes()]
            elif filters == [pikepdf.Name.FlateDecode] and "/DecodeParms" not in part:
                pieces = _inflate(part.read_raw_bytes())
            else:  # ponytail: other filters decode whole; rare in content streams
                pieces = [part.read_bytes()]
        except pikepdf.PdfError as exc:
            raise _content_unreadable() from exc
        for piece in pieces:
            _check_deadline(deadline)
            size += len(piece)
            window = tail + piece
            seen.update(token for token in (b"<<", b"BDC", b"DP") if token in window)
            tail = piece[-2:]
    return size, b"<<" in seen and (b"BDC" in seen or b"DP" in seen)


def _without_hidden_copies(doc, compiled_pattern, deadline: float):
    """Drop the copies of the text that no page draws; return the reopened doc.

    Owner's call (2026-10-03, after Codex and Muse disagreed): replacement text
    goes everywhere; a description (/Alt), a title, an id, a layer name and a
    page label lose only what the pattern matches, so a figure keeps its
    description for blind readers. Associated files (a Factur-X invoice),
    application data (Illustrator keeps the whole source file), actions and
    named destinations go whole, like the attachments, scripts and links
    scrub() removes. A key is judged by the object holding it, never by its
    name alone: a resource can be called /E or /AF.
    """
    import pikepdf

    # The preview ran the pattern on the page text only. A string here that it
    # cannot finish in time is emptied, not refused after payment; after the
    # first one every string is, or each would cost the timeout again.
    too_slow = False

    def sweep(holder, key) -> None:
        nonlocal too_slow
        value = holder[key] if isinstance(holder, pikepdf.Array) else holder.get(key)
        if not isinstance(value, pikepdf.String):
            return
        text = str(value)
        try:
            if too_slow:
                raise TimeoutError
            kept = compiled_pattern.sub("", text, timeout=REGEX_TIMEOUT_SECONDS)
        except TimeoutError:
            too_slow, kept = True, ""
        if kept != text:
            holder[key] = pikepdf.String(kept)

    swept: set[tuple[int, int]] = set()  # indirect containers, across every call

    def sweep_all(container) -> None:
        """Every string in a dictionary or array and in those inside it: a
        table's /Headers can be an array of its own object."""
        stack = [container]
        while stack:
            item = stack.pop()
            if item.is_indirect:
                if item.objgen in swept:
                    continue
                swept.add(item.objgen)
            keys = list(item.keys()) if isinstance(item, pikepdf.Dictionary) else range(len(item))
            for key in keys:
                value = item[key]
                if isinstance(value, (pikepdf.Dictionary, pikepdf.Array)):
                    stack.append(value)
                elif key != "/Lang":
                    sweep(item, key)

    def drop(holder, *keys) -> None:
        for key in keys:
            if key in holder:
                del holder[key]

    def clean_property_list(props) -> None:
        drop(props, *_REPLACEMENT_TEXT, "/AF")
        sweep_all(props)

    def tree_nodes(node):
        """The nodes of a name or number tree, each once."""
        stack, seen = [node], set()
        while stack:
            node = stack.pop()
            if not isinstance(node, pikepdf.Dictionary) or node.objgen in seen:
                continue
            if node.is_indirect:
                seen.add(node.objgen)
            yield node
            if isinstance(kids := node.get("/Kids"), pikepdf.Array):
                stack.extend(kids)

    with _pikepdf_open(doc.tobytes()) as pdf:
        root = pdf.Root
        drop(root, "/OpenAction", "/AA", "/Dests")
        if isinstance(names := root.get("/Names"), pikepdf.Dictionary):
            drop(names, "/Dests")
        threads = root.get("/Threads")
        for thread in threads if isinstance(threads, pikepdf.Array) else ():
            if not isinstance(thread, pikepdf.Dictionary):
                continue
            if isinstance(thread.get("/I"), pikepdf.Dictionary | pikepdf.Array):
                sweep_all(thread.I)  # an article's /Title and /Author
            else:
                sweep(thread, "/I")
        if isinstance(layers := root.get("/OCProperties"), pikepdf.Dictionary):
            sweep_all(layers)  # each configuration's /Name and /Creator, group labels
        for node in tree_nodes(root.get("/PageLabels")):
            if isinstance(nums := node.get("/Nums"), pikepdf.Array):
                for label in list(nums)[1::2]:
                    if isinstance(label, pikepdf.Dictionary):
                        sweep(label, "/P")

        content_streams = list(pdf.pages)
        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Dictionary | pikepdf.Stream):
                continue
            kind, subtype = obj.get("/Type"), obj.get("/Subtype")
            files = obj.get("/AF")
            if isinstance(files, pikepdf.Array) and all(
                isinstance(f, pikepdf.Dictionary | pikepdf.String) for f in files
            ):  # a colour space called /AF is an array that starts with a name
                del obj["/AF"]
            if kind in ("/Page", "/Catalog") or isinstance(obj, pikepdf.Stream):
                drop(obj, "/PieceInfo")  # a form's, and an image's: Illustrator keeps one there too
            if kind == "/Page" or "/FT" in obj or ("/Rect" in obj and subtype is not None):
                drop(obj, "/AA")
            if kind in ("/OCG", "/OCMD"):  # a layer is a property list too
                clean_property_list(obj)
            for resources in (obj, obj.get("/Resources")):
                props = resources.get("/Properties") if isinstance(
                    resources, pikepdf.Dictionary | pikepdf.Stream) else None
                for prop in props.values() if isinstance(props, pikepdf.Dictionary) else ():
                    if isinstance(prop, pikepdf.Dictionary):
                        clean_property_list(prop)
            if isinstance(obj, pikepdf.Stream) and (
                subtype == "/Form" or obj.get("/PatternType") == 1
            ):
                content_streams.append(obj)
            procs = obj.get("/CharProcs") if subtype == "/Type3" else None
            if isinstance(procs, pikepdf.Dictionary):
                content_streams.extend(procs.values())

        tree = root.get("/StructTreeRoot")
        if isinstance(tree, pikepdf.Dictionary):
            stack, seen = [tree.get("/K")], set()
            for node in tree_nodes(tree.get("/ParentTree")):  # elements /K never reaches
                if isinstance(nums := node.get("/Nums"), pikepdf.Array):
                    stack.extend(list(nums)[1::2])
            while stack:
                _check_deadline(deadline)
                elem = stack.pop()
                if isinstance(elem, pikepdf.Dictionary | pikepdf.Array) and elem.is_indirect:
                    if elem.objgen in seen:
                        continue  # a /K array holding itself looped forever
                    seen.add(elem.objgen)
                if isinstance(elem, pikepdf.Array):
                    stack.extend(elem)
                if not isinstance(elem, pikepdf.Dictionary) or "/S" not in elem:
                    continue  # a marked-content id or reference, not an element
                drop(elem, *_REPLACEMENT_TEXT)
                for key in ("/Alt", "/T", "/ID"):
                    sweep(elem, key)
                attributes = elem.get("/A")
                if not isinstance(attributes, pikepdf.Array):
                    attributes = [attributes]
                for attribute in attributes:
                    if isinstance(attribute, pikepdf.Dictionary):
                        sweep_all(attribute)  # table /Headers name element ids
                stack.append(elem.get("/K"))
            for node in tree_nodes(tree.get("/IDTree")):
                if isinstance(ids := node.get("/Names"), pikepdf.Array):
                    for index in range(0, len(ids), 2):
                        sweep(ids, index)
                if isinstance(limits := node.get("/Limits"), pikepdf.Array):
                    for index in range(len(limits)):
                        sweep(limits, index)
            if isinstance(classes := tree.get("/ClassMap"), pikepdf.Dictionary):
                sweep_all(classes)  # attributes an element takes by its /C

        for target in content_streams:
            source = target.obj if isinstance(target, pikepdf.Page) else target
            contents = source.get("/Contents") if isinstance(target, pikepdf.Page) else source
            parts = contents if isinstance(contents, pikepdf.Array) else [contents]
            size, marked = _scan_content(parts, deadline)
            if not marked:
                continue
            if size > _MAX_PARSED_CONTENT_BYTES:
                raise ApiError(
                    422,
                    "content_too_complex",
                    "Este PDF tem uma página demasiado pesada para garantirmos que o texto "
                    "censurado sai de todas as cópias. " + _REDACT_AS_IMAGES,
                )
            try:
                instructions = list(pikepdf.parse_content_stream(target))
            except Exception as exc:  # MuPDF draws past a bad token; pikepdf stops
                raise _content_unreadable() from exc
            changed = False
            for index, instruction in enumerate(instructions):
                operands = instruction.operands
                if str(instruction.operator) in ("BDC", "DP") and operands and isinstance(
                    operands[-1], pikepdf.Dictionary
                ):
                    before = operands[-1].unparse()
                    clean_property_list(operands[-1])
                    if operands[-1].unparse() != before:
                        instructions[index] = pikepdf.ContentStreamInstruction(
                            operands, instruction.operator
                        )
                        changed = True
            if changed:
                data = pikepdf.unparse_content_stream(instructions)
                if isinstance(target, pikepdf.Page):
                    source.Contents = pdf.make_stream(data)
                else:
                    target.write(data)

        buf = io.BytesIO()
        pdf.save(buf)
    doc.close()
    return pymupdf.open(stream=buf.getvalue(), filetype="pdf")


def check_hidden_copies(doc, *, strategy: str, custom_text: str, regex_pattern: str) -> None:
    """The preview runs the apply's hidden-copy pass on a copy of doc, so what
    it refuses (content_unreadable, content_too_complex) is refused before the
    customer pays, not after."""
    pattern_str, flags = _compile_pattern(strategy, custom_text, regex_pattern)
    copy = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    try:
        deadline = time.monotonic() + REDACTION_SCAN_TIMEOUT_SECONDS
        copy = _without_hidden_copies(copy, regex.compile(pattern_str, flags), deadline)
    finally:
        copy.close()


def _without_xref_holes(doc):
    """scrub() reads every object number and raises on one no xref section
    defines. That is valid PDF — the object is null — and pyHanko-signed files
    and pdf-lib saves have them: Censurar answered 500. Renumbering closes them.
    """
    for xref in range(1, doc.xref_length()):
        try:
            doc.xref_object(xref)
        except Exception:
            clean = pymupdf.open(stream=doc.tobytes(garbage=2), filetype="pdf")
            doc.close()
            return clean
    return doc


def redact_pdf(
    content: bytes,
    *,
    strategy: str,
    custom_text: str = "",
    regex_pattern: str = "",
    confirmed_ids: list[str] | None = None,
) -> bytes:
    """Apply PII redaction. If confirmed_ids is None, redact every match.
    If it is a list, refuse unknown ids, then redact the listed matches plus
    every match past PREVIEW_MATCH_CAP. The user never saw those, so could not
    have deselected them: 200 of 5 200 emails used to survive "redaction".

    Covered image pixels are blanked too: on a scan with an OCR layer the box
    used to hide the text layer only, and the email stayed readable in the
    page image under it.
    """
    doc = _open_pdf(content)
    try:
        with REDACTION_LOCK:
            deadline = time.monotonic() + PROCESSING_BUDGET_SECONDS
            all_matches = _extract_matches(
                doc,
                strategy=strategy,
                custom_text=custom_text,
                regex_pattern=regex_pattern,
                deadline=deadline,
            )

            if confirmed_ids is not None:
                confirmed_set = set(confirmed_ids)
                if confirmed_set - {m.id for m in all_matches}:
                    raise ApiError(
                        409,
                        "preview_stale",
                        "A pré-visualização já não corresponde a este PDF. "
                        "Faça uma nova pré-visualização e confirme novamente os dados a censurar.",
                    )
                matches_to_apply = [
                    m
                    for index, m in enumerate(all_matches)
                    if index >= PREVIEW_MATCH_CAP or m.id in confirmed_set
                ]
            else:
                matches_to_apply = all_matches

            pattern_str, flags = _compile_pattern(strategy, custom_text, regex_pattern)
            doc = _without_hidden_copies(doc, regex.compile(pattern_str, flags), deadline)

            if matches_to_apply:
                pages_with_redactions = sorted({m.page for m in matches_to_apply})
                _check_image_budget(doc, pages=pages_with_redactions)
                pymupdf.TOOLS.set_small_glyph_heights(True)
                try:
                    for m in matches_to_apply:
                        doc[m.page].add_redact_annot(pymupdf.Rect(*m.bbox), fill=(0, 0, 0))
                    _apply_redactions(doc, pages_with_redactions, deadline)
                finally:
                    pymupdf.TOOLS.set_small_glyph_heights(False)

        # CRITICAL: strip residual sensitive data from outline/metadata.
        # Without this, the "redacted" PDF can still leak the redacted content via
        # bookmark titles (EU AstraZeneca 2021) or metadata (multiple). See spec for
        # full citations. Runs UNCONDITIONALLY — even a no-match round-trip must
        # strip metadata. Annotations and form fields were already baked into the
        # page (their matched text is gone with the rest). hidden_text=False: in
        # PyMuPDF 1.27 it removed nothing (it needs "3 Tr" alone on a line), and
        # where it did work it would strip a scan's whole OCR layer; the matched
        # invisible characters are removed by apply_redactions like any other.
        doc = _without_xref_holes(doc)
        doc.scrub(
            attached_files=True,
            embedded_files=True,
            hidden_text=False,
            javascript=True,
            metadata=True,
            xml_metadata=True,
            remove_links=True,
            reset_fields=True,
            reset_responses=True,
            thumbnails=True,
            clean_pages=True,
            redactions=False,   # already applied above with explicit options
            redact_images=0,
        )
        # scrub() does NOT touch the document outline (verified against pymupdf
        # 1.27 docs). Clear the TOC explicitly so bookmark titles cannot leak
        # — this is the exact EU AstraZeneca 2021 failure mode.
        doc.set_toc([])
        # A certification signature (/Perms) and validation data (/DSS) keep the
        # signer's certificate and contact details, which a redacted email can
        # be. Every signature is invalid after the rewrite anyway.
        for key in ("Perms", "DSS"):
            doc.xref_set_key(doc.pdf_catalog(), key, "null")

        output = doc.tobytes(garbage=4, deflate=True, clean=True)
        if len(output) > MAX_RESPONSE_BYTES:
            raise ApiError(
                422,
                "output_too_large",
                "O PDF censurado ficaria com mais de 30 MB, o máximo que conseguimos "
                "entregar. Comprima primeiro o PDF com a ferramenta Comprimir PDF e "
                "censure o resultado.",
            )
        return output
    except ApiError:
        raise
    except Exception as exc:
        logger.warning("redact failed: %r", exc)
        raise ApiError(500, "redaction_failed", "Não foi possível censurar este PDF.") from exc
    finally:
        doc.close()


def _docx_is_effectively_empty(path: Path) -> bool:
    """True when the produced docx has no non-whitespace text and no tables.

    pdf2docx swallows per-page errors (ignore_page_error=True) and exits 0,
    so a scanned/degraded PDF yields a valid-but-empty docx no exception flags.
    """
    from docx import Document

    document = Document(str(path))
    has_text = any(p.text.strip() for p in document.paragraphs)
    return not has_text and len(document.tables) == 0


_CONTROL_CHARS = _re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # not allowed in XML 1.0

# Page turn that makes a dominant text direction (unrotated coordinates) run
# left to right.
_UPRIGHT_ROTATION = {(1, 0): 0, (0, -1): 90, (-1, 0): 180, (0, 1): 270}


def _turn_text_upright(doc) -> bool:
    """Turn each page so its main text runs left to right; True if any changed.

    pdf2docx keeps only horizontal text: pages shown with /Rotate 90 or 180 —
    a landscape table stored as a turned portrait page — lost all their text.
    """
    changed = False
    for page in doc:
        weights: dict[tuple[int, int], int] = {}
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", ()):
                key = (round(line["dir"][0]), round(line["dir"][1]))
                weights[key] = weights.get(key, 0) + sum(len(s["text"]) for s in line["spans"])
        angle = _UPRIGHT_ROTATION.get(max(weights, key=weights.get)) if weights else None
        if angle is None or (angle == 0 and page.rotation == 0):
            continue
        page.set_rotation(angle)
        if angle:
            page.remove_rotation()  # bake the turn into the content
        changed = True
    return changed


def _text_layer_chars(doc) -> tuple[int, int]:
    """(visible, invisible) characters; OCR layers are invisible (render mode 3)."""
    visible = invisible = 0
    for page in doc:
        for span in page.get_texttrace():
            if span["type"] == 3:
                invisible += len(span["chars"])
            else:
                visible += len(span["chars"])
    return visible, invisible


def _docx_from_text_layer(doc) -> bytes:
    """A Word file from the text layer, one paragraph per text block.

    pdf2docx skips invisible (OCR) text, so a scan run through our own OCR came
    back «use the OCR tool first» — the step the user had just taken. This
    keeps the words and the page breaks, not the layout.
    """
    from docx import Document

    word = Document()
    for pno, page in enumerate(doc):
        if pno:
            word.add_page_break()
        for block in page.get_text("blocks", sort=True):
            text = _CONTROL_CHARS.sub("", " ".join(block[4].split()))
            if block[6] == 0 and text:
                word.add_paragraph(text)
    buf = io.BytesIO()
    word.save(buf)
    return buf.getvalue()


def pdf_to_docx(content: bytes) -> bytes:
    """Convert a text-based PDF to an editable .docx via the pdf2docx CLI.

    Guard order is load-bearing: encrypted before page access, page cap before
    the get_text loop, then the scanned/empty-text gate.
    """
    doc = _open_pdf(content)
    try:
        if doc.page_count > MAX_PAGES:
            raise ApiError(
                status_code=422,
                code="too_many_pages",
                message=(
                    f"PDF demasiado grande (máximo: {MAX_PAGES} páginas). "
                    "Divida-o antes de converter."
                ),
            )
        visible, invisible = _text_layer_chars(doc)
        if visible < doc.page_count * 10:
            if invisible >= doc.page_count * 10:
                return _docx_from_text_layer(doc)  # a scan with an OCR layer
            raise ApiError(
                status_code=422,
                code="scanned_pdf",
                message=(
                    "Este PDF parece digitalizado (sem texto selecionável). "
                    "Use a ferramenta OCR primeiro e depois converta."
                ),
            )
        # pdf2docx inspects every vector path hunting for table borders, so
        # its cost tracks path count — not pages, not bytes. Measured on
        # 10-page files against the 45s subprocess budget:
        #
        #   vector items |  2 010 | 15 010 | 30 010 | 40 010 | 50 010
        #   convert time |  0.6 s |  2.5 s | 10.0 s | 19.4 s | 30.7 s
        #
        # Superlinear, and on hardware faster than the Cloud Run container.
        # A larger file of 180 plain-text pages carries zero paths and
        # converts in 7.8 s, which is why this counts paths and not size.
        #
        # This is the shape behind four 504s on 2026-08-13: one 1.4 MB PDF,
        # four retries in five minutes, every one dying at ~55 s. Counting
        # costs 0.03 s, so saying no quickly is nearly free.
        vector_items = sum(
            len(drawing.get("items", ()))
            for page in doc
            for drawing in page.get_cdrawings()
        )
        if vector_items > get_settings().max_vector_items:
            raise ApiError(
                status_code=422,
                code="pdf_too_complex",
                message=(
                    "Este PDF tem demasiados elementos gráficos para converter "
                    "para Word dentro do tempo limite. Divida-o primeiro com a "
                    "ferramenta Dividir PDF e converta cada parte."
                ),
            )
        _check_image_budget(doc)
        source = doc.tobytes() if _turn_text_upright(doc) else content
    finally:
        doc.close()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        input_path = Path(tmpdir) / "input.pdf"
        output_path = Path(tmpdir) / "output.docx"
        input_path.write_bytes(source)

        result = _run_command(
            ["pdf2docx", "convert", str(input_path), str(output_path)],
            timeout=TOOL_SUBPROCESS_TIMEOUT,
            tmpdir=tmpdir,
        )
        skipped = sorted(
            {
                int(page)
                for page in _re.findall(
                    r"Ignore page (\d+) due to (?:parsing|making) page error:", result.stderr
                )
            }
        )
        if skipped:
            pages = ", ".join(map(str, skipped))
            location = f"na página {pages}" if len(skipped) == 1 else f"nas páginas {pages}"
            raise ApiError(
                422,
                "conversion_incomplete",
                f"Falhou a conversão para Word {location}. "
                "Reveja as páginas indicadas. Use «Extrair PDF» para selecionar "
                "as restantes e convertê-las em separado.",
            )
        if result.returncode != 0 or not output_path.exists():
            logger.error("pdf2docx failed: %s", _trim_process_output(result.stderr))
            raise ApiError(
                status_code=500,
                code="conversion_failed",
                message="Falha na conversão do PDF para Word.",
            )

        if _docx_is_effectively_empty(output_path):
            raise ApiError(
                status_code=422,
                code="scanned_pdf",
                message=(
                    "Não foi possível extrair texto deste PDF. "
                    "Se for digitalizado, use a ferramenta OCR primeiro."
                ),
            )
        # pdf2docx stores photos as lossless PNG: 7 photo pages made 126 MB.
        if output_path.stat().st_size > MAX_RESPONSE_BYTES:
            raise ApiError(
                422,
                "output_too_large",
                "O documento Word ficaria com mais de 30 MB, o máximo que conseguimos "
                "entregar. Comprima primeiro o PDF com a ferramenta Comprimir PDF e "
                "converta o resultado.",
            )

        return output_path.read_bytes()


def _sheet_title(pno: int, ti: int) -> str:
    """Build a worksheet title for the table `ti` on page `pno`.

    openpyxl forbids ``* ? : / [ ] \\`` and caps titles at 31 chars; this format
    uses none of those and stays well within 31 chars for MAX_PAGES/MAX_TABLES.
    Duplicates are auto-deduped by openpyxl; the [:31] slice is defensive only.
    """
    return f"Pag {pno} Tabela {ti}"[:31]


# Cell text that can only mean one number or date becomes one; the rest stays
# text. "1.234" (PT thousands or EN decimal), "12,500", "2.10" (a section
# number) stay text, as do leading zeros (codes, postcodes) and runs of more
# than 10 digits (account numbers; Excel would show 1.23E+11).
_INT_RE = _re.compile(r"-?(?:0|[1-9]\d{0,9})")
_PT_DECIMAL_RE = _re.compile(r"-?\d{1,3}(?:\.\d{3})+,\d+|-?\d+,(?:\d{1,2}|\d{4,})")
_EN_DECIMAL_RE = _re.compile(r"-?\d{1,3}(?:,\d{3})+\.\d+")
_THOUSANDS_RE = _re.compile(r"-?\d{1,3}(?:\.\d{3}){2,}|-?\d{1,3}(?:,\d{3}){2,}")
_SPACED_RE = _re.compile(r"-?\d{1,3}(?:[ \u00a0]\d{3})+(?:,\d+)?")  # 1 234 567,89
_DMY_RE = _re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")
_ISO_DATE_RE = _re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _decimal_format(decimals: int) -> str:
    return "#,##0." + "0" * decimals if decimals else "#,##0"


def _cell_value(text: str) -> tuple[object, str | None]:
    """(value, number_format): a number or date when the text is unambiguous."""
    import datetime

    s = text.strip()
    if not s or len(s) > 32:
        return text, None
    if _INT_RE.fullmatch(s):
        return int(s), None
    if _PT_DECIMAL_RE.fullmatch(s):
        return float(s.replace(".", "").replace(",", ".")), _decimal_format(len(s.split(",")[1]))
    if _EN_DECIMAL_RE.fullmatch(s):
        return float(s.replace(",", "")), _decimal_format(len(s.split(".")[1]))
    if _THOUSANDS_RE.fullmatch(s):
        return int(s.replace(".", "").replace(",", "")), "#,##0"
    if _SPACED_RE.fullmatch(s):
        compact = s.replace(" ", "").replace("\u00a0", "")
        if "," in compact:
            return float(compact.replace(",", ".")), _decimal_format(len(compact.split(",")[1]))
        return int(compact), "#,##0"
    for pattern, (y, m, d) in ((_DMY_RE, (2, 1, 0)), (_ISO_DATE_RE, (0, 1, 2))):
        match = pattern.fullmatch(s)
        if match:
            parts = [int(g) for g in match.groups()]
            try:
                return datetime.date(parts[y], parts[m], parts[d]), "dd/mm/yyyy"
            except ValueError:
                return text, None
    return text, None


def pdf_to_xlsx(content: bytes) -> bytes:
    """Extract tables from a text PDF into an .xlsx (one sheet per table).

    Guard order is load-bearing: invalid -> encrypted -> page-cap (all cheap,
    before the loop) -> per-page complexity -> per-table cells-cap. The
    scanned-vs-no-tables decision is deferred until after extraction so a sparse
    legit table is not pre-rejected by the text-density gate. The whole body is
    wrapped in try/except -> ApiError(500) because main.py has no catch-all
    handler, so a raw find_tables()/openpyxl exception would escape as a
    code-less plain-text 500 that the FE cannot render.
    """
    from io import BytesIO

    from openpyxl import Workbook

    doc = _open_pdf(content)
    try:
        # --- Cheap pre-flight (reuse pdf_to_docx codes/messages) ---
        if doc.page_count > MAX_PAGES:
            raise ApiError(
                422,
                "too_many_pages",
                f"PDF demasiado grande (máximo: {MAX_PAGES} páginas). Divida-o antes de converter.",
            )

        deadline = time.monotonic() + PROCESSING_BUDGET_SECONDS
        wb = Workbook()
        wb.remove(wb.active)  # start with zero sheets
        n_tables, n_cells = 0, 0
        for pno, page in enumerate(doc, start=1):
            _check_deadline(deadline)
            # Complexity: reject a vector-graphics bomb before find_tables' O(n²) clustering.
            if len(page.get_cdrawings()) > MAX_PATHS_PER_PAGE:
                raise ApiError(
                    422,
                    "pdf_too_complex",
                    "Página demasiado complexa para extrair tabelas com segurança.",
                )
            for ti, tab in enumerate(page.find_tables().tables, start=1):
                rows = tab.extract()
                if not rows:
                    continue
                n_tables += 1
                if n_tables > MAX_TABLES:
                    raise ApiError(
                        422,
                        "too_many_tables",
                        f"Este PDF tem mais de {MAX_TABLES} tabelas. "
                        "Divida-o com a ferramenta Dividir PDF e converta cada parte.",
                    )
                n_cells += sum(len(r) for r in rows)
                if n_cells > MAX_CELLS:  # memory cap
                    raise ApiError(
                        422,
                        "pdf_too_complex",
                        "Demasiadas células para um só ficheiro.",
                    )
                ws = wb.create_sheet(title=_sheet_title(pno, ti))
                for r_idx, row in enumerate(rows, start=1):
                    for c_idx, val in enumerate(row, start=1):
                        # One control character (a broken ToUnicode map) used to
                        # fail the whole file with openpyxl's IllegalCharacterError.
                        text = _CONTROL_CHARS.sub("", "" if val is None else str(val))
                        value, number_format = _cell_value(text)
                        cell = ws.cell(row=r_idx, column=c_idx, value=value)
                        # Neutralize formula ('f') / error ('e') injection: a cell
                        # like "=HYPERLINK(...)" would otherwise ship as a live
                        # formula, and "=1+1"/"#REF!" would render as an error.
                        if cell.data_type in ("f", "e"):
                            cell.data_type = "s"
                        elif number_format:
                            cell.number_format = number_format

        if n_tables == 0:
            # Decide scanned-vs-no-tables AFTER extraction: a sparse legit table
            # must not be pre-rejected by the text-density gate, and a scanned
            # PDF yields zero tables and lands here anyway.
            total_chars = sum(len("".join(p.get_text().split())) for p in doc)
            if total_chars < doc.page_count * 10:
                # Tables are found from drawn lines: an OCR text layer adds none,
                # so «OCR first» sold a paid step that could not help.
                raise ApiError(
                    422,
                    "scanned_pdf",
                    "Não foi possível extrair texto deste PDF. A conversão para Excel só "
                    "encontra tabelas em PDFs criados digitalmente; num PDF digitalizado "
                    "não as encontra, nem depois do OCR.",
                )
            raise ApiError(
                422,
                "no_tables_detected",
                "Não encontrámos tabelas neste PDF. Esta ferramenta extrai tabelas; "
                "para converter texto corrido use o PDF para Word.",
            )

        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except ApiError:
        raise
    except Exception as exc:
        logger.error("pdf_to_xlsx failed: %s", exc)
        raise ApiError(
            500,
            "conversion_failed",
            "Falha na conversão do PDF para Excel.",
        ) from exc
    finally:
        doc.close()


# --- reparar-pdf -------------------------------------------------------------

_REPAIR_UNRECOVERABLE = "Não foi possível reparar o PDF — está demasiado danificado."


def repair_pdf(content: bytes) -> tuple[bytes, dict[str, str]]:
    """Repair a corrupt PDF. Returns (output_bytes, X-Repair-* headers).

    Tier-1 pikepdf work runs in a memory+time-bounded SUBPROCESS (app.services.
    _repair_worker) so a decompression bomb expands in a killable child, never in
    this API worker. On escalation the parent runs the (already sandboxed) gs Tier-2.
    """
    # Pre-check: magic bytes (liveness only — cheap, no decompression). Cut
    # anything before the header: a leading %!PS program with "%PDF-" in a
    # comment passed the old substring test and ran in Ghostscript (Tier 2).
    content = content[_pdf_start(content) :]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        in_path = os.path.join(tmp, "in.pdf")
        out_path = os.path.join(tmp, "out.pdf")
        meta_path = os.path.join(tmp, "meta.json")
        with open(in_path, "wb") as fh:
            fh.write(content)
        cmd = [sys.executable, "-m", "app.services._repair_worker", in_path, out_path, meta_path]
        result, stderr = _run_guarded(
            cmd,
            timeout=REPAIR_WORKER_TIMEOUT,
            mem_bytes=GS_REPAIR_MEM_BYTES,
            tmpdir=tmp,
        )
        if result == "timeout":
            raise ApiError(status_code=422, code="repair_timeout",
                           message="O PDF é demasiado complexo para reparar no tempo disponível.")
        if result == "oom":
            raise ApiError(status_code=422, code="repair_oom",
                           message="O PDF é demasiado grande ou complexo para reparar.")
        if result != "ok" or not os.path.exists(meta_path):
            logger.warning("repair worker failed (result=%s): %s", result, stderr[:2000])
            raise ApiError(status_code=422, code="unrecoverable_pdf", message=_REPAIR_UNRECOVERABLE)
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("repair worker wrote unreadable meta: %s", exc)
            raise ApiError(
                status_code=422,
                code="unrecoverable_pdf",
                message=_REPAIR_UNRECOVERABLE,
            ) from exc

        outcome = meta.get("outcome")
        if outcome == "ok":
            out_bytes = Path(out_path).read_bytes()
            return out_bytes, meta["headers"]
        if outcome == "error":
            raise ApiError(status_code=meta["status"], code=meta["code"], message=meta["message"])
        # outcome == "escalate" — baseline captured; run Tier-2 outside the tempdir
        baseline = meta.get("baseline")

    return _repair_with_ghostscript(content, baseline)


REPAIR_WORKER_TIMEOUT = 12  # Tier-1 bomb guard; total repair budget remains below the proxy.
GS_REPAIR_TIMEOUT = 30  # Tier-2; 12 + 30 leaves transport/serialization headroom.
GS_REPAIR_MAX_BYTES = 30 * 1024 * 1024       # do not spend the gs budget on >30MB
GS_REPAIR_MEM_BYTES = 1536 * 1024 * 1024     # 1.5 GiB rlimit, under the 2Gi container


def _run_guarded(
    cmd: list[str], *, timeout: int, mem_bytes: int, tmpdir: str
) -> tuple[str, str]:
    """Run a repair step with a memory cap in its own process group.

    Returns (result, stderr) with result in ok | timeout | oom | failed.
    """
    try:
        proc = _spawn(cmd, timeout=timeout, tmpdir=tmpdir, mem_bytes=mem_bytes, text=False)
    except FileNotFoundError as exc:
        raise ApiError(status_code=503, code="tool_unavailable",
                       message=TOOL_UNAVAILABLE_MESSAGE) from exc
    except subprocess.TimeoutExpired:
        return "timeout", ""
    if proc.returncode == 0:
        return "ok", ""
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    out_of_memory = (
        "VMerror" in stderr
        or "out of memory" in stderr.lower()
        or "MemoryError" in stderr
        or proc.returncode == -signal.SIGKILL
    )
    if out_of_memory:
        return "oom", stderr
    return "failed", stderr


def _all_pages_blank(pdf_bytes: bytes) -> bool:
    """No text, image or drawing on any page."""
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            return not any(
                page.get_text().strip() or page.get_images() or page.get_cdrawings()
                for page in doc
            )
    except Exception:
        return False


def _repair_with_ghostscript(content: bytes, baseline: int | None) -> tuple[bytes, dict[str, str]]:
    import pikepdf

    if len(content) > GS_REPAIR_MAX_BYTES:
        raise ApiError(status_code=422, code="repair_too_large",
                       message="O PDF é demasiado grande para reparação profunda.")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        in_path = os.path.join(tmp, "in.pdf")
        out_path = os.path.join(tmp, "out.pdf")
        with open(in_path, "wb") as fh:
            fh.write(content)
        cmd = [
            "gs", "-q", "-dSAFER", "-dNOPAUSE", "-dBATCH",
            "-dDetectDuplicateImages=false",
            "-sDEVICE=pdfwrite", "-o", out_path, in_path,
        ]
        result, stderr = _run_guarded(
            cmd, timeout=GS_REPAIR_TIMEOUT, mem_bytes=GS_REPAIR_MEM_BYTES, tmpdir=tmp
        )
        if result == "timeout":
            raise ApiError(status_code=422, code="repair_timeout",
                           message="O PDF é demasiado complexo para reparar no tempo disponível.")
        if result == "oom":
            raise ApiError(status_code=422, code="repair_oom",
                           message="O PDF é demasiado grande ou complexo para reparar.")
        if result != "ok" or not os.path.exists(out_path):
            logger.warning("ghostscript repair failed (result=%s): %s", result, stderr[:2000])
            raise ApiError(status_code=422, code="unrecoverable_pdf", message=_REPAIR_UNRECOVERABLE)
        out_bytes = Path(out_path).read_bytes()

    try:
        reopened = pikepdf.open(io.BytesIO(out_bytes))
    except pikepdf.PdfError as exc:
        raise ApiError(
            status_code=422,
            code="unrecoverable_pdf",
            message=_REPAIR_UNRECOVERABLE,
        ) from exc
    try:
        k = len(reopened.pages)  # re-derived from the gs OUTPUT, not the input object
    finally:
        reopened.close()
    if k == 0 or (not baseline and _all_pages_blank(out_bytes)):
        # gs "reinterprets" a header plus random bytes (no readable page at all)
        # as one blank page; that is nothing recovered, not a repair to sell.
        # A file whose declared pages were blank to begin with stays a repair.
        raise ApiError(status_code=422, code="unrecoverable_pdf", message=_REPAIR_UNRECOVERABLE)

    pages_header = f"{k}/{baseline}" if baseline is not None else str(k)
    return out_bytes, {
        "X-Repair-Status": "reinterpreted-lossy",
        "X-Repair-Method": "ghostscript",
        "X-Repair-Pages": pages_header,
        "X-Repair-Warnings": "true",
    }
