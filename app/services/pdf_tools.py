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
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
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
MAX_OCR_PAGES = 8  # two rounds of OCR_JOBS
# The pages cap alone let 6 photo pages with captions through: OCRmyPDF renders
# any page with text or a vector drawing — a scanner's footer is enough — at
# 400 dpi, and the time goes into writing those pixels as PNG, twice per page.
# So the pixels it will render are budgeted too, colour counting twice
# (_ocr_megapixels). A page never splits and waits for a free worker, so the
# busiest worker sets the time: it may render MAX_OCR_MEGAPIXELS / OCR_JOBS.
# Seconds of processing on Cloud Run gen2, 4 vCPU / 4 GiB, 2026-09-24:
#   8 grey A4 scans (69.6 Mpx) 17 · 8 colour A4 scans (139.3) 28.5
#   4 A4 photos with captions (123.7) 39-40 · 8 A4 photos at 300 dpi (139.3) ~45
# The last sits at the 45 s kill. Gen1 on 2 vCPU ran ~3x slower than the 2 CPU
# bench box and cut 8 colour scans at 45 s; gen2 alone gained only 11-20 %.
MAX_OCR_MEGAPIXELS = 150
# Hand Tesseract at most ~A4 at 300 dpi; the 400 dpi renders above it only
# cost time (31 → 25 s on those photo pages). Scans up to 300 dpi are untouched.
OCR_DOWNSAMPLE_FLAGS = (
    "--tesseract-downsample-large-images",
    "--tesseract-downsample-above",
    "3600",
)

CONFORMANCE_MAP = {
    "pdfa-1b": "1",
    "pdfa-2b": "2",
    "pdfa-3b": "3",
}

REGEX_TIMEOUT_SECONDS = 0.5

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
                jpeg_boxes = _jpeg_image_boxes(page)
                page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                )
                _store_redacted_jpegs_as_jpeg(page, jpeg_boxes)
    finally:
        pymupdf.TOOLS.set_small_glyph_heights(False)
    doc.bake(annots=True, widgets=True)

    seen_ids: set[str] = set()
    for page_idx, page in enumerate(doc):
        _check_deadline(deadline, _SCAN_TIMEOUT_MESSAGE)
        text = page.get_text("text")
        # First spelling of each match, in reading order. Case variants collapse
        # (search_for is case-insensitive and would box the same place twice),
        # and whitespace collapses to one space: search_for spans line breaks
        # and non-breaking spaces, a literal "\n" or "\xa0" in the needle not.
        needles: dict[str, str] = {}
        try:
            for regex_match in compiled_pattern.finditer(text, timeout=REGEX_TIMEOUT_SECONDS):
                needle = " ".join(regex_match.group().split())
                if needle:
                    needles.setdefault(needle.casefold(), needle)
        except regex.error as exc:
            raise ApiError(400, "invalid_regex_pattern", "Padrão regex inválido.") from exc
        except TimeoutError as exc:
            raise ApiError(
                400, "regex_too_slow",
                "O padrão regex é demasiado complexo (possível ReDoS). Simplifique-o.",
            ) from exc

        for needle in needles.values():
            _check_deadline(deadline, _SCAN_TIMEOUT_MESSAGE)
            for rect in page.search_for(needle):
                # Decompose the match rect into per-word bboxes for clean preview highlights.
                words = page.get_text("words", clip=rect)
                boxes = (
                    [(w[0], w[1], w[2], w[3], w[4]) for w in words]
                    if words
                    else [(rect.x0, rect.y0, rect.x1, rect.y1, needle)]
                )
                for x0, y0, x1, y1, word_text in boxes:
                    match = _make_match(strategy, page_idx, (x0, y0, x1, y1), word_text, needle)
                    if match.id in seen_ids:
                        continue
                    seen_ids.add(match.id)
                    yield match


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
    for i in range(0, len(points) - 3, 4):
        piece = page.add_redact_annot(pymupdf.Quad(points[i : i + 4]).rect, cross_out=False)
        for key in ("IC", "DA", "OverlayText", "Q"):
            kind, value = doc.xref_get_key(mark.xref, key)
            if kind != "null":
                doc.xref_set_key(
                    piece.xref, key, pymupdf.get_pdf_str(value) if kind == "string" else value
                )
    page.delete_annot(mark)


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
    password_protected_pdf; no pages, only partly readable, or pages MuPDF does
    not see (unless hidden_pages_ok) → 422 damaged_pdf.
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
        if not hidden_pages_ok:
            _refuse_hidden_pages(doc, content)
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


def _refuse_hidden_pages(doc, content: bytes) -> None:
    """422 when the page tree's /Kids hold more pages than MuPDF counts.

    MuPDF takes the number of pages from the tree's /Count; qpdf, Ghostscript and
    viewers follow /Kids. /Count 3 over 4 pages hid page 4 from every tool while
    the saved file still showed it: Censurar left its email readable. Walked
    without recursion, each /Pages node once, a repeated kid counted each time;
    past MAX_PAGE_TREE_WALK objects qpdf counts instead, in its own process.
    """
    size, limit = doc.xref_length(), doc.page_count
    pages, walked = 0, 0
    try:
        kind, root = doc.xref_get_key(doc.pdf_catalog(), "Pages")
        stack = [(int(root.split()[0]), 1)] if kind == "xref" else []
        nodes = set()
        while stack and pages <= limit and walked < MAX_PAGE_TREE_WALK:
            xref, times = stack.pop()
            walked += 1
            if not 0 < xref < size:
                continue
            kind, kids = doc.xref_get_key(xref, "Kids")
            if kind == "null":
                # a dictionary, not a stream: what qpdf and pdf.js show as a page
                if not doc.xref_is_stream(xref) and doc.xref_get_keys(xref):
                    pages += times
            elif xref not in nodes:  # a /Pages node, walked once: a cycle ends here
                nodes.add(xref)
                if kind == "xref":  # an indirect /Kids array
                    kids = doc.xref_object(int(kids.split()[0]))
                refs = Counter(m[1] for m in _re.finditer(r"(\d+) \d+ R", kids))
                stack += [(int(kid), n) for kid, n in refs.items()]
    except Exception:
        return  # unreadable tree: the damaged-file checks after this decide
    if stack and pages <= limit:  # too big to walk here
        counted = _qpdf_page_count(content)
        pages = limit + 1 if counted is None else counted  # cannot tell: refuse
    if pages > limit:
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

    try:
        try:
            return img2pdf.convert(_sanitize_image(content))
        except Image.DecompressionBombError:
            raise
        except Exception:
            return img2pdf.convert(_reencode_frames(content))
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
        if len(pages) > MAX_OCR_PAGES:
            raise ApiError(
                422,
                "too_many_pages",
                f"Este PDF tem {len(pages)} páginas para reconhecer e o OCR processa até "
                f"{MAX_OCR_PAGES} de cada vez. Separe-as com a ferramenta Extrair PDF, "
                f"até {MAX_OCR_PAGES} páginas de cada vez, e processe cada parte.",
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
        if _busiest_ocr_worker(megapixels) > worker_budget:
            fit = sum(
                1
                for n in range(1, len(pages) + 1)
                if _busiest_ocr_worker(megapixels[:n]) <= worker_budget
            )
            raise ApiError(
                422,
                "too_many_pages",
                f"Este PDF tem {len(pages)} páginas para reconhecer, a cores ou em alta "
                f"resolução, e o OCR processa até {fit} páginas assim de cada vez. "
                f"Separe-as com a ferramenta Extrair PDF, até {fit} de cada vez, e "
                "processe cada parte.",
            )
        _check_image_budget(doc, pages=[pno - 1 for pno in pages])
        all_pages = len(pages) == doc.page_count
        # --redo-ocr refuses a PDF with fillable form fields (exit 2); --skip-text
        # accepts it, and the pages chosen above have no real text to skip.
        mode = "--skip-text" if doc.is_form_pdf else "--redo-ocr"
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
                "-l",
                lang_code,
                *([] if all_pages else ["--pages", ",".join(map(str, pages))]),
                str(input_path),
                str(output_path),
            ],
            timeout=TOOL_SUBPROCESS_TIMEOUT,
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

        return output_path.read_bytes()


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


def redact_pdf(
    content: bytes,
    *,
    strategy: str,
    custom_text: str = "",
    regex_pattern: str = "",
    confirmed_ids: list[str] | None = None,
) -> bytes:
    """Apply PII redaction. If confirmed_ids is None, redact every match.
    If it is a list, redact the listed matches (unknown ids are skipped — the
    user's preview saw a set; we trust intent) plus every match past the
    preview's PREVIEW_MATCH_CAP, which the user never saw and so could not
    have deselected: 200 of 5 200 emails used to survive "redaction".

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
                matches_to_apply = [
                    m
                    for index, m in enumerate(all_matches)
                    if index >= PREVIEW_MATCH_CAP or m.id in confirmed_set
                ]
            else:
                matches_to_apply = all_matches

            if matches_to_apply:
                pages_with_redactions = sorted({m.page for m in matches_to_apply})
                _check_image_budget(doc, pages=pages_with_redactions)
                pymupdf.TOOLS.set_small_glyph_heights(True)
                try:
                    for m in matches_to_apply:
                        doc[m.page].add_redact_annot(pymupdf.Rect(*m.bbox), fill=(0, 0, 0))

                    for page_idx in pages_with_redactions:
                        _check_deadline(deadline)
                        page = doc[page_idx]
                        jpeg_boxes = _jpeg_image_boxes(page)
                        page.apply_redactions(
                            images=pymupdf.PDF_REDACT_IMAGE_PIXELS,  # blank what the box covers
                            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                            text=pymupdf.PDF_REDACT_TEXT_REMOVE,  # explicit: delete characters
                        )
                        _store_redacted_jpegs_as_jpeg(page, jpeg_boxes)
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
                n_cells += sum(len(r) for r in rows)
                if n_cells > MAX_CELLS:  # memory cap
                    raise ApiError(
                        422,
                        "pdf_too_complex",
                        "Demasiadas células para um só ficheiro.",
                    )
                n_tables += 1
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
                if n_tables >= MAX_TABLES:
                    break
            if n_tables >= MAX_TABLES:
                break

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
