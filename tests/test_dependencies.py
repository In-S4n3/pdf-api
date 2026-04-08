"""Tests that all PDF library dependencies are functional.

Each test proves a library can perform its core operation,
not just that it imports. These tests run inside the Docker
container where system dependencies are installed.
"""

import subprocess
from pathlib import Path

import pikepdf
import pymupdf

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_pymupdf_loads_pdf(sample_pdf_path):
    """PyMuPDF can open and read a PDF file."""
    doc = pymupdf.open(str(sample_pdf_path))
    assert len(doc) >= 1
    doc.close()


def test_pymupdf_creates_pdf():
    """PyMuPDF can create a new PDF from scratch."""
    doc = pymupdf.open()
    page = doc.new_page()
    assert page.rect.width > 0
    doc.close()


def test_pikepdf_loads_pdf(sample_pdf_path):
    """pikepdf can open and read a PDF file."""
    pdf = pikepdf.open(str(sample_pdf_path))
    assert len(pdf.pages) >= 1
    pdf.close()


def test_pikepdf_creates_pdf():
    """pikepdf can create a new PDF from scratch."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    assert len(pdf.pages) == 1
    pdf.close()


def test_ghostscript_installed():
    """Ghostscript binary is available and runs."""
    result = subprocess.run(
        ["gs", "--version"], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
    assert result.stdout.strip()


def test_tesseract_installed():
    """Tesseract binary is available with Portuguese language support."""
    result = subprocess.run(
        ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
    assert "por" in result.stdout


def test_tesseract_english_installed():
    """Tesseract has English language pack."""
    result = subprocess.run(
        ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
    assert "eng" in result.stdout


def test_libreoffice_installed():
    """LibreOffice binary is available in headless mode."""
    result = subprocess.run(
        ["soffice", "--headless", "--version"],
        capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    assert "LibreOffice" in result.stdout
