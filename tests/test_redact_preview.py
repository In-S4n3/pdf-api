"""POST /v2/redact/preview returns JSON match list (no PDF body)."""

import io
import json
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
FIXTURE = Path(__file__).parent / "fixtures" / "sample_with_email.pdf"


def _post_preview(strategy="email", custom="", pattern=""):
    files = {"file": ("sample.pdf", io.BytesIO(FIXTURE.read_bytes()), "application/pdf")}
    return client.post(
        "/v2/redact/preview",
        files=files,
        data={
            "options": (
                f'{{"strategy":"{strategy}","customText":"{custom}","regexPattern":"{pattern}"}}'
            )
        },
        headers={"X-API-Key": "test-key"},
    )


def test_preview_returns_json_with_matches():
    r = _post_preview()
    assert r.status_code == 200, r.text
    body = r.json()
    assert "matches" in body and "total" in body
    assert body["total"] >= 2


def test_preview_matches_have_required_fields():
    r = _post_preview()
    for m in r.json()["matches"]:
        assert "id" in m and len(m["id"]) == 16
        assert "page" in m and isinstance(m["page"], int)
        assert "bbox" in m and len(m["bbox"]) == 4
        assert "kind" in m
        assert "context" in m
        assert "fullMatch" in m


def test_preview_no_matches_returns_empty_list():
    r = _post_preview(strategy="custom", custom="nothingmatches")
    assert r.status_code == 200
    body = r.json()
    assert body["matches"] == []
    assert body["total"] == 0


def test_preview_invalid_regex_400():
    r = _post_preview(strategy="regex", pattern="[unclosed")
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "invalid_regex_pattern"


def test_preview_encrypted_pdf_400():
    files = {
        "file": (
            "e.pdf",
            io.BytesIO((Path(__file__).parent / "fixtures" / "encrypted.pdf").read_bytes()),
            "application/pdf",
        )
    }
    r = client.post(
        "/v2/redact/preview",
        files=files,
        data={"options": '{"strategy":"email"}'},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "password_protected_pdf"


def test_apply_with_confirmed_ids_round_trips():
    """Preview returns IDs; apply with subset of those IDs redacts only the subset."""
    preview = _post_preview(strategy="email")
    matches = preview.json()["matches"]
    alice_ids = [m["id"] for m in matches if "alice" in m["fullMatch"]]
    assert alice_ids

    import json

    files = {"file": ("sample.pdf", io.BytesIO(FIXTURE.read_bytes()), "application/pdf")}
    r = client.post(
        "/v2/redact",
        files=files,
        data={"options": json.dumps({"strategy": "email", "confirmed_ids": alice_ids})},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200, r.text

    import pymupdf

    with pymupdf.open(stream=r.content, filetype="pdf") as doc:
        text = "\n".join(p.get_text("text") for p in doc)
    assert "alice" not in text.lower(), text
    assert "bob@example.org" in text  # bob NOT redacted (not in confirmed_ids)


def test_preview_missing_custom_text_returns_422_not_500():
    """Regression: model_validator's ValueError used to leak into FastAPI's
    response serializer and produce a 500. Verify it now returns a clean 422."""
    files = {"file": ("sample.pdf", io.BytesIO(FIXTURE.read_bytes()), "application/pdf")}
    r = client.post(
        "/v2/redact/preview",
        files=files,
        data={"options": '{"strategy":"custom","customText":""}'},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "invalid_options"
    # details must serialize cleanly — no ValueError object in payload
    assert isinstance(body["error"]["details"], list)


def test_preview_missing_regex_pattern_returns_422_not_500():
    files = {"file": ("sample.pdf", io.BytesIO(FIXTURE.read_bytes()), "application/pdf")}
    r = client.post(
        "/v2/redact/preview",
        files=files,
        data={"options": '{"strategy":"regex","regexPattern":""}'},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_options"


def test_apply_missing_custom_text_returns_422_not_500():
    """Same regression on the apply path."""
    files = {"file": ("sample.pdf", io.BytesIO(FIXTURE.read_bytes()), "application/pdf")}
    r = client.post(
        "/v2/redact",
        files=files,
        data={"options": '{"strategy":"custom","customText":""}'},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_options"


def test_preview_rejects_a_pdf_above_the_shared_page_limit():
    """Bound preview CPU before a pathological document monopolises the worker."""
    doc = pymupdf.open()
    for _ in range(201):
        doc.new_page()
    content = doc.tobytes()
    doc.close()

    r = client.post(
        "/v2/redact/preview",
        files={"file": ("many-pages.pdf", io.BytesIO(content), "application/pdf")},
        data={"options": json.dumps({"strategy": "email"})},
        headers={"X-API-Key": "test-key"},
    )

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "too_many_pages"


def test_fill_form_is_deprecated_without_removing_the_endpoint(sample_pdf):
    """Deprecation remains non-destructive during the announced sunset window."""
    r = client.post(
        "/v2/fill-form",
        files={"file": ("plain.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": json.dumps({"fields": {"Name": "Tiago"}})},
        headers={"X-API-Key": "test-key"},
    )

    assert r.headers["deprecation"] == "@1787875200"
    assert "sunset" not in r.headers
    assert "rel=\"deprecation\"" in r.headers["link"]


@pytest.mark.parametrize("scan", [False, True], ids=["text", "scan"])
@pytest.mark.parametrize("crop", [None, (100, 50, 500, 750)], ids=["mediabox", "cropbox"])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_preview_boxes_sit_where_the_turned_page_shows_the_match(rotation, crop, scan):
    """The UI draws each box as a share of the page as shown, turned by /Rotate
    (react-pdf's viewport); the boxes came unturned, over other words. And on a
    CropBox away from 0 0, the black box itself went off the email."""
    from app.router_v2 import _extract_matches_json
    from app.services.pdf_tools import redact_pdf

    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        if scan:  # a blank scan under its OCR layer: PyMuPDF paints that box
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 10), False)
            pixmap.clear_with(255)
            page.insert_image(pymupdf.Rect(120, 270, 480, 320), pixmap=pixmap)
        page.insert_text((150, 300), "Mail ana@example.com end", fontsize=14,
                         render_mode=3 if scan else 0)
        if crop:
            page.set_cropbox(pymupdf.Rect(crop))
        page.set_rotation(rotation)
        source = doc.tobytes()
    [match] = _extract_matches_json(
        source, strategy="email", custom_text="", regex_pattern="", match_cap=10)["matches"]
    output = redact_pdf(source, strategy="email", confirmed_ids=[match["id"]])
    with pymupdf.open(stream=output, filetype="pdf") as doc:
        pix = doc[0].get_pixmap()  # the page as shown, 1 pixel per point
        assert (pix.width, pix.height) == (round(doc[0].rect.width), round(doc[0].rect.height))
        x0, y0, x1, y1 = (round(v) for v in match["bbox"])
        inside = [pix.pixel(x, y)[0] for x in range(x0 + 1, x1 - 1) for y in range(y0 + 1, y1 - 1)]
        assert inside and max(inside) < 100, "the black box is where the preview drew it"
    # Painted over is not removed: the email is gone from the text too.
    from tests.test_redact_hidden_copies import _drawn_text
    from tests.test_redact_neighbours import _no_match_survives

    _no_match_survives(output, "ana@example.com")
    assert "Mail" in _drawn_text(output) and "end" in _drawn_text(output)
