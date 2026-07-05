"""Tests for the unlock_pdf service + POST /v2/pdf-unlock route.

unlock_pdf removes password/permission encryption. Unlike every other tool it
ACCEPTS an encrypted input. The two real-world cases it must cover:
  1. owner-only PDF (empty user password, printing/copy restricted) -> opens
     with NO password; save strips restrictions. This is the common case.
  2. user-password PDF -> needs the supplied open password.
"""

import io
import json

import pikepdf
import pymupdf
import pytest

from app.api_errors import ApiError
from app.services import pdf_tools
from app.services.pdf_tools import unlock_pdf

# -- fixtures (generated in-test, like test_protect.py) ----------------------


def _make_plain_pdf(pages: int = 1) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Plain page {i + 1}", fontsize=12)
    result = doc.tobytes()
    doc.close()
    return result


def _make_user_pw_pdf(password: str, pages: int = 1) -> bytes:
    """Encrypted with a real USER (open) password — needs the password to open."""
    pdf = pikepdf.open(io.BytesIO(_make_plain_pdf(pages)))
    buf = io.BytesIO()
    pdf.save(buf, encryption=pikepdf.Encryption(owner=password, user=password, R=6, aes=True))
    pdf.close()
    return buf.getvalue()


def _make_owner_only_pdf(pages: int = 1) -> bytes:
    """Owner password set, USER password EMPTY, restrictive permissions.

    Opens with no password; pikepdf can strip the restrictions on save. This is
    the dominant 'locked PDF' users bring to an unlock tool.
    """
    pdf = pikepdf.open(io.BytesIO(_make_plain_pdf(pages)))
    buf = io.BytesIO()
    pdf.save(
        buf,
        encryption=pikepdf.Encryption(
            owner="owner-secret",
            user="",
            R=6,
            aes=True,
            allow=pikepdf.Permissions(extract=False, modify_other=False, modify_annotation=False),
        ),
    )
    pdf.close()
    return buf.getvalue()


def _is_decrypted(result: bytes) -> bool:
    assert isinstance(result, bytes) and len(result) > 0
    pdf = pikepdf.open(io.BytesIO(result))  # opens with no password -> proves decrypted
    try:
        return not pdf.is_encrypted
    finally:
        pdf.close()


# -- service: happy paths ----------------------------------------------------


def test_owner_only_unlocks_without_password():
    """The common case: restriction-only PDF unlocks with NO password."""
    assert _is_decrypted(unlock_pdf(_make_owner_only_pdf()))


def test_user_pw_unlocks_with_correct_password():
    assert _is_decrypted(unlock_pdf(_make_user_pw_pdf("segredo"), "segredo"))


def test_owner_only_ignores_a_supplied_password():
    """A password on an owner-only file is harmless (open succeeds without it)."""
    assert _is_decrypted(unlock_pdf(_make_owner_only_pdf(), "whatever"))


# -- service: error paths ----------------------------------------------------


def test_user_pw_without_password_raises_password_required():
    with pytest.raises(ApiError) as exc:
        unlock_pdf(_make_user_pw_pdf("segredo"))
    assert exc.value.status_code == 400
    assert exc.value.code == "password_required"


def test_user_pw_wrong_password_raises_wrong_password():
    with pytest.raises(ApiError) as exc:
        unlock_pdf(_make_user_pw_pdf("segredo"), "errada")
    assert exc.value.status_code == 400
    assert exc.value.code == "wrong_password"


def test_plain_pdf_raises_not_encrypted():
    """Non-encrypted input must be a clean 422, never a silent no-op re-save."""
    with pytest.raises(ApiError) as exc:
        unlock_pdf(_make_plain_pdf())
    assert exc.value.status_code == 422
    assert exc.value.code == "not_encrypted"


def test_plain_pdf_with_password_still_not_encrypted():
    """pikepdf opens unencrypted PDFs silently even with a password -> still 422."""
    with pytest.raises(ApiError) as exc:
        unlock_pdf(_make_plain_pdf(), "irrelevant")
    assert exc.value.code == "not_encrypted"


def test_too_many_pages(monkeypatch):
    monkeypatch.setattr(pdf_tools, "MAX_PAGES", 1)
    with pytest.raises(ApiError) as exc:
        unlock_pdf(_make_owner_only_pdf(pages=3))
    assert exc.value.status_code == 400
    assert exc.value.code == "too_many_pages"


def test_garbage_input_raises_unsupported_not_500():
    """Malformed/unsupported input -> graceful 422 (pikepdf.PdfError), never 500."""
    with pytest.raises(ApiError) as exc:
        unlock_pdf(b"%PDF-1.4 this is not a real pdf")
    assert exc.value.status_code == 422
    assert exc.value.code == "unsupported_encryption"


def test_retry_open_pdferror_maps_to_422_not_500(monkeypatch):
    """Correct password but a PdfError on the RETRY open must be a clean 422.

    A sibling except clause does not catch an exception raised inside another
    handler, so without explicit handling this would escape as an unhandled 500
    and break the v2 error envelope.
    """
    calls = {"n": 0}

    def fake_open(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise pikepdf.PasswordError("needs a password")
        raise pikepdf.PdfError("corrupt after decrypt")

    monkeypatch.setattr(pikepdf, "open", fake_open)
    with pytest.raises(ApiError) as exc:
        unlock_pdf(b"irrelevant", "somepass")
    assert exc.value.status_code == 422
    assert exc.value.code == "unsupported_encryption"


# -- route: POST /v2/pdf-unlock ----------------------------------------------


def test_route_owner_only_returns_pdf(client):
    resp = client.post(
        "/v2/pdf-unlock",
        files={"file": ("locked.pdf", io.BytesIO(_make_owner_only_pdf()), "application/pdf")},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert _is_decrypted(resp.content)


def test_route_user_pw_with_password_returns_pdf(client):
    resp = client.post(
        "/v2/pdf-unlock",
        files={"file": ("locked.pdf", io.BytesIO(_make_user_pw_pdf("segredo")), "application/pdf")},
        data={"options": json.dumps({"password": "segredo"})},
    )
    assert resp.status_code == 200
    assert _is_decrypted(resp.content)


def test_route_wrong_password_structured_400(client):
    resp = client.post(
        "/v2/pdf-unlock",
        files={"file": ("locked.pdf", io.BytesIO(_make_user_pw_pdf("segredo")), "application/pdf")},
        data={"options": json.dumps({"password": "errada"})},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "wrong_password"


def test_route_password_required_structured_400(client):
    resp = client.post(
        "/v2/pdf-unlock",
        files={"file": ("locked.pdf", io.BytesIO(_make_user_pw_pdf("segredo")), "application/pdf")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "password_required"


def test_route_not_encrypted_structured_422(client):
    resp = client.post(
        "/v2/pdf-unlock",
        files={"file": ("plain.pdf", io.BytesIO(_make_plain_pdf()), "application/pdf")},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "not_encrypted"
