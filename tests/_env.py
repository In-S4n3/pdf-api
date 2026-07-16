"""Capability probes for the Docker-only integration tests.

The /ocr and /pdfa endpoints and the dependency smoke-tests need the full
server toolchain — tesseract language packs, unpaper, LibreOffice, and
Ghostscript's PDF/A resources — that ships in the Docker image but is absent on
a bare dev box. These probes let those tests SKIP off-image instead of
hard-failing, so `pytest` stays green everywhere and still runs for real inside
the container where the binaries exist.
"""
from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path


@functools.lru_cache(maxsize=1)
def tesseract_langs() -> frozenset[str]:
    """Language codes tesseract reports, or empty if tesseract is absent."""
    if not shutil.which("tesseract"):
        return frozenset()
    try:
        out = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    # Some builds print the header to stderr and codes to stdout, others merge
    # both to stdout — union them. Codes have no spaces; the header line does.
    lines = (out.stdout + "\n" + out.stderr).splitlines()
    return frozenset(s.strip() for s in lines if s.strip() and " " not in s.strip())


def has_soffice() -> bool:
    """LibreOffice headless binary."""
    return shutil.which("soffice") is not None


def can_ocr(lang: str = "eng") -> bool:
    """The /ocr route runs ocrmypdf with --deskew --clean (needs unpaper) plus the
    requested tesseract language pack. All ship in the Docker image; a dev box
    usually lacks unpaper (and the non-English packs)."""
    return (
        shutil.which("ocrmypdf") is not None
        and shutil.which("unpaper") is not None
        and lang in tesseract_langs()
    )


def pdfa_resources_present() -> bool:
    """The exact precondition convert_pdf_to_pdfa() checks before running:
    Ghostscript's PDF/A definition template + the default RGB ICC profile."""
    gs_root = Path("/usr/share/ghostscript")
    icc = Path("/usr/share/color/icc/ghostscript/default_rgb.icc")
    has_def = gs_root.is_dir() and next(gs_root.rglob("PDFA_def.ps"), None) is not None
    return has_def and icc.exists()
