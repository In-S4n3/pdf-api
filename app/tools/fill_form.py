"""Fill form endpoint -- fills AcroForm fields and flattens to static PDF.

Uses pikepdf's high-level Form class to fill text, checkbox, radio, and
dropdown/choice fields. Appearance streams are generated via
ExtendedAppearanceStreamGenerator before flattening so filled values
render correctly in all PDF viewers.
"""

import io
import json

import pikepdf
from pikepdf.form import (
    CheckboxField,
    ChoiceField,
    ExtendedAppearanceStreamGenerator,
    Form,
    MultipleFieldProxy,
    RadioButtonGroup,
    TextField,
)

from fastapi import APIRouter, Depends, File, Form as FastAPIForm, HTTPException, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key

router = APIRouter()


def _set_field_value(field, value) -> None:
    """Set a form field value based on its type.

    Handles TextField, CheckboxField, RadioButtonGroup, ChoiceField,
    and MultipleFieldProxy (duplicate field names).
    """
    if isinstance(field, MultipleFieldProxy):
        # Multiple widgets share the same name -- set each one
        for sub_field in field:
            _set_field_value(sub_field, value)
        return

    if isinstance(field, CheckboxField):
        field.checked = bool(value)
    elif isinstance(field, RadioButtonGroup):
        # Try to match option by label or value
        str_value = str(value)
        for opt in field.options:
            if str(opt.on_value) == str_value or str(opt.on_value) == f"/{str_value}":
                opt.select()
                return
        # Fallback: try setting value directly as pikepdf.Name
        field.value = pikepdf.Name(f"/{str_value}")
    elif isinstance(field, ChoiceField):
        field.value = str(value)
    elif isinstance(field, TextField):
        field.value = str(value)
    else:
        # Unknown field type -- attempt string value as fallback
        field.value = str(value)


@router.post("/fill-form")
async def fill_form(
    file: UploadFile = File(...),
    options: str = FastAPIForm("{}"),
    _key: str = Depends(verify_api_key),
) -> Response:
    """Accept a PDF and field values, return filled+flattened PDF."""
    opts = json.loads(options)
    field_values = opts.get("fields", {})

    if not isinstance(field_values, dict) or not field_values:
        raise HTTPException(status_code=400, detail="No field values provided")

    content = await file.read()

    try:
        pdf = pikepdf.open(io.BytesIO(content))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to process form")

    # Check if PDF has AcroForm
    if pdf.Root.get("/AcroForm") is None:
        pdf.close()
        raise HTTPException(status_code=400, detail="PDF has no form fields")

    try:
        # Create high-level Form with appearance stream generator
        form = Form(pdf, generate_appearances=ExtendedAppearanceStreamGenerator)

        # Fill fields -- skip unknown field names defensively
        for field_name, value in field_values.items():
            try:
                field = form[field_name]
            except KeyError:
                continue

            _set_field_value(field, value)

        # Flatten all form fields into static content
        # IMPORTANT: Do NOT call pdf.check() -- known pikepdf bug #506
        pdf.flatten_annotations("all")

        # Save result
        buf = io.BytesIO()
        pdf.save(buf)
        result = buf.getvalue()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to process form")
    finally:
        pdf.close()

    filename = file.filename or "output.pdf"
    return Response(
        content=result,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
