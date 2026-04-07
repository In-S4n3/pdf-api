"""Tests for POST /protect endpoint."""

import io
import json

import pikepdf
import pymupdf


def _make_plain_pdf() -> bytes:
    """Create a simple test PDF with text content."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Test document for protection", fontsize=12)
    result = doc.tobytes()
    doc.close()
    return result


def _make_encrypted_pdf(password: str) -> bytes:
    """Create an already-encrypted PDF using pikepdf."""
    # First create a plain PDF
    plain = _make_plain_pdf()
    pdf = pikepdf.open(io.BytesIO(plain))
    buf = io.BytesIO()
    pdf.save(
        buf,
        encryption=pikepdf.Encryption(
            owner=password, user=password, R=6, aes=True,
        ),
    )
    pdf.close()
    return buf.getvalue()


# -- Happy path --


def test_protect_returns_encrypted_pdf(client):
    """POST /protect with PDF + valid password returns 200 with application/pdf."""
    pdf_bytes = _make_plain_pdf()
    response = client.post(
        "/protect",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"userPassword": "secret123"})},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_protect_pdf_requires_password_to_open(client):
    """Encrypted PDF cannot be opened without password (pikepdf.PasswordError)."""
    pdf_bytes = _make_plain_pdf()
    response = client.post(
        "/protect",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"userPassword": "secret123"})},
    )
    assert response.status_code == 200
    import pytest
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(io.BytesIO(response.content))


def test_protect_pdf_opens_with_correct_password(client):
    """Opening encrypted PDF with correct password succeeds and is_encrypted=True."""
    pdf_bytes = _make_plain_pdf()
    response = client.post(
        "/protect",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"userPassword": "mypass"})},
    )
    assert response.status_code == 200
    pdf = pikepdf.open(io.BytesIO(response.content), password="mypass")
    assert pdf.is_encrypted
    pdf.close()


def test_protect_print_allowed(client):
    """Encrypted PDF has print_lowres=True and print_highres=True."""
    pdf_bytes = _make_plain_pdf()
    response = client.post(
        "/protect",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"userPassword": "testpw"})},
    )
    assert response.status_code == 200
    pdf = pikepdf.open(io.BytesIO(response.content), password="testpw")
    assert pdf.allow.print_lowres is True
    assert pdf.allow.print_highres is True
    pdf.close()


def test_protect_extract_blocked(client):
    """Encrypted PDF has extract=False (text/image copy blocked)."""
    pdf_bytes = _make_plain_pdf()
    response = client.post(
        "/protect",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"userPassword": "testpw"})},
    )
    assert response.status_code == 200
    pdf = pikepdf.open(io.BytesIO(response.content), password="testpw")
    assert pdf.allow.extract is False
    pdf.close()


def test_protect_preserves_filename(client):
    """Content-Disposition includes the original filename."""
    pdf_bytes = _make_plain_pdf()
    response = client.post(
        "/protect",
        files={"file": ("my-document.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"userPassword": "testpw"})},
    )
    assert response.status_code == 200
    assert "my-document.pdf" in response.headers.get("content-disposition", "")


# -- Error cases --


def test_protect_empty_password_returns_400(client):
    """POST /protect with empty password string returns 400."""
    pdf_bytes = _make_plain_pdf()
    response = client.post(
        "/protect",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"userPassword": ""})},
    )
    assert response.status_code == 400


def test_protect_whitespace_password_returns_400(client):
    """POST /protect with whitespace-only password returns 400."""
    pdf_bytes = _make_plain_pdf()
    response = client.post(
        "/protect",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"userPassword": "   "})},
    )
    assert response.status_code == 400


def test_protect_no_options_returns_400(client):
    """POST /protect with no options (defaults to empty password) returns 400."""
    pdf_bytes = _make_plain_pdf()
    response = client.post(
        "/protect",
        files={"file": ("test.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({})},
    )
    assert response.status_code == 400


def test_protect_missing_file_returns_422(client):
    """POST /protect without file returns 422 (FastAPI validation)."""
    response = client.post(
        "/protect",
        data={"options": json.dumps({"userPassword": "test"})},
    )
    assert response.status_code == 422
