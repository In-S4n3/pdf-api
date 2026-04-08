"""Tests for the v2 HTTP contract."""

import io
import json

import pymupdf
from PIL import Image


def _make_pdf_with_text_field(field_name: str) -> bytes:
    """Create a PDF with a single text field for v2 form tests."""
    doc = pymupdf.open()
    page = doc.new_page()
    widget = pymupdf.Widget()
    widget.field_name = field_name
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.rect = pymupdf.Rect(72, 72, 300, 100)
    page.add_widget(widget)
    result = doc.tobytes()
    doc.close()
    return result


def _minimal_jpeg() -> bytes:
    """Create a small JPEG image for conversion tests."""
    buf = io.BytesIO()
    image = Image.new("RGB", (10, 10), color=(255, 0, 0))
    image.save(buf, format="JPEG")
    return buf.getvalue()


def test_v2_echo_returns_request_id_header(client, sample_pdf):
    """Successful v2 responses include a request id header."""
    response = client.post(
        "/v2/echo",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"]
    assert response.content == sample_pdf


def test_v2_invalid_options_returns_structured_error(client, sample_pdf):
    """Malformed JSON options produce the new structured error envelope."""
    response = client.post(
        "/v2/ocr",
        files={"file": ("test.pdf", io.BytesIO(sample_pdf), "application/pdf")},
        data={"options": "{"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_options"
    assert body["error"]["requestId"] == response.headers["x-request-id"]


def test_v2_missing_file_returns_structured_validation_error(client):
    """FastAPI validation errors should use the v2 envelope."""
    response = client.post("/v2/echo")
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert isinstance(body["error"]["details"], list)


def test_v2_pdf_to_image_invalid_pdf_returns_structured_error(client):
    """Invalid PDF input should be reported consistently in v2."""
    response = client.post(
        "/v2/pdf-to-image",
        files={"file": ("bad.pdf", io.BytesIO(b"not a pdf"), "application/pdf")},
        data={"options": json.dumps({"format": "png"})},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_pdf"


def test_v2_fill_form_rejects_unknown_fields(client):
    """v2 form filling should fail loudly when the field mapping is wrong."""
    pdf_bytes = _make_pdf_with_text_field("Name")
    response = client.post(
        "/v2/fill-form",
        files={"file": ("form.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"fields": {"MissingField": "John Doe"}})},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "unknown_form_fields"
    assert body["error"]["details"]["unknownFields"] == ["MissingField"]


def test_v2_convert_can_infer_type_from_filename(client):
    """v2 conversion should work even when the part MIME type is empty."""
    response = client.post(
        "/v2/convert",
        files={"file": ("photo.jpg", io.BytesIO(_minimal_jpeg()), "")},
        data={"options": "{}"},
    )
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"
