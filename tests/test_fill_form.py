"""Tests for POST /fill-form endpoint."""

import io
import json

import pymupdf


def _make_pdf_with_text_field(field_name: str, default_value: str = "") -> bytes:
    """Create a PDF with a single AcroForm text field using pymupdf."""
    doc = pymupdf.open()
    page = doc.new_page()
    widget = pymupdf.Widget()
    widget.field_name = field_name
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.rect = pymupdf.Rect(72, 72, 300, 100)
    widget.field_value = default_value
    page.add_widget(widget)
    result = doc.tobytes()
    doc.close()
    return result


def _make_pdf_with_checkbox(field_name: str, checked: bool = False) -> bytes:
    """Create a PDF with a single AcroForm checkbox field using pymupdf."""
    doc = pymupdf.open()
    page = doc.new_page()
    widget = pymupdf.Widget()
    widget.field_name = field_name
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
    widget.rect = pymupdf.Rect(72, 72, 90, 90)
    widget.field_value = checked
    page.add_widget(widget)
    result = doc.tobytes()
    doc.close()
    return result


def _make_pdf_with_multiple_text_fields(fields: dict[str, str]) -> bytes:
    """Create a PDF with multiple AcroForm text fields."""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for name, value in fields.items():
        widget = pymupdf.Widget()
        widget.field_name = name
        widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        widget.rect = pymupdf.Rect(72, y, 300, y + 28)
        widget.field_value = value
        page.add_widget(widget)
        y += 40
    result = doc.tobytes()
    doc.close()
    return result


def _make_plain_pdf() -> bytes:
    """Create a PDF with no form fields (just text)."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "No form fields here", fontsize=12)
    result = doc.tobytes()
    doc.close()
    return result


def _extract_text(content: bytes) -> str:
    """Extract all text from a PDF response body."""
    doc = pymupdf.open(stream=content, filetype="pdf")
    text = ""
    for page in doc:
        text += page.get_text("text")
    doc.close()
    return text


def _has_acroform(content: bytes) -> bool:
    """Check if PDF still has interactive AcroForm fields."""
    import pikepdf

    pdf = pikepdf.open(io.BytesIO(content))
    acroform = pdf.Root.get("/AcroForm")
    has_fields = False
    if acroform is not None:
        fields = acroform.get("/Fields")
        has_fields = fields is not None and len(fields) > 0
    pdf.close()
    return has_fields


# -- Happy path --


def test_fill_text_field(client):
    """Fill a text field and verify value appears in flattened output."""
    pdf_bytes = _make_pdf_with_text_field("Name")
    response = client.post(
        "/fill-form",
        files={"file": ("form.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"fields": {"Name": "John Doe"}})},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    text = _extract_text(response.content)
    assert "John Doe" in text


def test_fill_flattens_form(client):
    """Filled PDF has no interactive form fields after flattening."""
    pdf_bytes = _make_pdf_with_text_field("Name")
    response = client.post(
        "/fill-form",
        files={"file": ("form.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"fields": {"Name": "Test"}})},
    )
    assert response.status_code == 200
    assert not _has_acroform(response.content)


def test_fill_checkbox_field(client):
    """Fill a checkbox field with True value."""
    pdf_bytes = _make_pdf_with_checkbox("Agree", checked=False)
    response = client.post(
        "/fill-form",
        files={"file": ("form.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"fields": {"Agree": True}})},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_fill_multiple_text_fields(client):
    """Fill multiple text fields in one request."""
    pdf_bytes = _make_pdf_with_multiple_text_fields(
        {"FirstName": "", "LastName": "", "City": ""}
    )
    response = client.post(
        "/fill-form",
        files={"file": ("form.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={
            "options": json.dumps(
                {
                    "fields": {
                        "FirstName": "Maria",
                        "LastName": "Silva",
                        "City": "Lisboa",
                    }
                }
            )
        },
    )
    assert response.status_code == 200
    text = _extract_text(response.content)
    assert "Maria" in text
    assert "Silva" in text
    assert "Lisboa" in text


def test_fill_preserves_filename(client):
    """Content-Disposition includes the original filename."""
    pdf_bytes = _make_pdf_with_text_field("Name")
    response = client.post(
        "/fill-form",
        files={"file": ("my-form.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"fields": {"Name": "Test"}})},
    )
    assert response.status_code == 200
    assert "my-form.pdf" in response.headers.get("content-disposition", "")


def test_fill_returns_valid_pdf(client):
    """Response is 200 with application/pdf content type."""
    pdf_bytes = _make_pdf_with_text_field("Name")
    response = client.post(
        "/fill-form",
        files={"file": ("form.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"fields": {"Name": "Hello"}})},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    # Verify it starts with PDF header
    assert response.content[:5] == b"%PDF-"


# -- Error cases --


def test_fill_no_fields_returns_400(client):
    """Empty fields dict returns 400."""
    pdf_bytes = _make_pdf_with_text_field("Name")
    response = client.post(
        "/fill-form",
        files={"file": ("form.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"fields": {}})},
    )
    assert response.status_code == 400


def test_fill_missing_fields_key_returns_400(client):
    """Options without 'fields' key returns 400."""
    pdf_bytes = _make_pdf_with_text_field("Name")
    response = client.post(
        "/fill-form",
        files={"file": ("form.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({})},
    )
    assert response.status_code == 400


def test_fill_no_acroform_returns_400(client):
    """PDF without form fields returns 400."""
    pdf_bytes = _make_plain_pdf()
    response = client.post(
        "/fill-form",
        files={"file": ("plain.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={"options": json.dumps({"fields": {"Name": "Test"}})},
    )
    assert response.status_code == 400


def test_fill_missing_file_returns_422(client):
    """POST without file returns 422 (FastAPI validation)."""
    response = client.post(
        "/fill-form",
        data={"options": json.dumps({"fields": {"Name": "Test"}})},
    )
    assert response.status_code == 422


def test_fill_nonexistent_field_skipped(client):
    """Field name not in PDF is silently skipped; valid fields still filled."""
    pdf_bytes = _make_pdf_with_text_field("Name")
    response = client.post(
        "/fill-form",
        files={"file": ("form.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        data={
            "options": json.dumps(
                {"fields": {"Name": "John Doe", "NonExistent": "skip me"}}
            )
        },
    )
    assert response.status_code == 200
    text = _extract_text(response.content)
    assert "John Doe" in text
