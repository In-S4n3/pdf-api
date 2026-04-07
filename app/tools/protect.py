"""Protect endpoint -- encrypts PDF with AES-256 password protection.

Uses pikepdf's Encryption API with R=6 (AES-256) to apply password
protection with restricted permissions: printing allowed, editing
and text/image extraction blocked.
"""

import io
import json

import pikepdf
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.auth import verify_api_key

router = APIRouter()

PROTECT_PERMISSIONS = pikepdf.Permissions(
    accessibility=True,
    extract=False,
    modify_annotation=False,
    modify_assembly=False,
    modify_form=False,
    modify_other=False,
    print_lowres=True,
    print_highres=True,
)


@router.post("/protect")
async def protect(
    file: UploadFile = File(...),
    options: str = Form("{}"),
    _key: str = Depends(verify_api_key),
) -> Response:
    """Accept a PDF and password, return an AES-256 encrypted PDF."""
    opts = json.loads(options)
    password = opts.get("userPassword", "")

    # Validate password is non-empty and non-whitespace
    if not password or not password.strip():
        raise HTTPException(status_code=400, detail="Password is required")

    content = await file.read()

    try:
        pdf = pikepdf.open(io.BytesIO(content))
    except pikepdf.PasswordError:
        raise HTTPException(
            status_code=400,
            detail="Este PDF ja esta protegido com palavra-passe",
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to open PDF")

    try:
        buf = io.BytesIO()
        pdf.save(
            buf,
            encryption=pikepdf.Encryption(
                owner=password,
                user=password,
                R=6,
                aes=True,
                allow=PROTECT_PERMISSIONS,
            ),
        )
        result = buf.getvalue()
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to protect PDF")
    finally:
        pdf.close()

    filename = file.filename or "output.pdf"
    return Response(
        content=result,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
