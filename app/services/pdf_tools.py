"""Shared PDF processing services for v1 and v2 routes."""

from __future__ import annotations

import io
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

import img2pdf

from app.api_errors import ApiError

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

IMAGE_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
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
    "portuguese": "por",
    "italian": "ita",
    "chinese": "chi_sim",
    "jpn": "jpn",
}

CONFORMANCE_MAP = {
    "pdfa-1b": "1",
    "pdfa-2b": "2",
    "pdfa-3b": "3",
}

EMAIL_PATTERN = r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
PHONE_PATTERN = r"[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{7,15}"
PATTERNS = {
    "email": EMAIL_PATTERN,
    "phone": PHONE_PATTERN,
}

VALID_REDACTION_STRATEGIES = ("email", "phone", "custom", "regex")
MAX_REGEX_LENGTH = 500

REDACTION_LOCK = threading.Lock()


def _trim_process_output(value: str, limit: int = 500) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[-limit:]


def _run_command(
    command: list[str],
    *,
    timeout: int,
    missing_message: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ApiError(
            status_code=503,
            code="tool_unavailable",
            message=missing_message,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ApiError(
            status_code=504,
            code="processing_timeout",
            message="The processing tool timed out before completing the request.",
        ) from exc


def _resolve_convert_content_type(content_type: str | None, filename: str | None) -> str | None:
    if content_type:
        normalized = content_type.lower()
        if normalized in OFFICE_MIMES or normalized in IMAGE_MIMES:
            return normalized

    suffix = Path(filename or "").suffix.lower()
    return EXTENSION_TO_MIME.get(suffix)


def compress_pdf(content: bytes) -> bytes:
    """Compress a PDF using PyMuPDF."""
    import pymupdf

    doc = None
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
        doc.rewrite_images(
            dpi_threshold=150,
            dpi_target=96,
            quality=75,
        )
        return doc.tobytes(
            garbage=4,
            deflate=True,
            clean=True,
            use_objstms=True,
        )
    except Exception as exc:
        raise ApiError(
            status_code=400,
            code="invalid_pdf",
            message="Nao foi possivel abrir o PDF. Verifique se o ficheiro e valido.",
        ) from exc
    finally:
        if doc is not None:
            doc.close()


def flatten_pdf(content: bytes) -> bytes:
    """Flatten annotations and form fields into the page content."""
    import pymupdf

    doc = None
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
        doc.bake(annots=True, widgets=True)
        return doc.tobytes(garbage=4, deflate=True)
    except Exception as exc:
        raise ApiError(
            status_code=400,
            code="invalid_pdf",
            message="Nao foi possivel abrir o PDF. Verifique se o ficheiro e valido.",
        ) from exc
    finally:
        if doc is not None:
            doc.close()


def _convert_office(content: bytes, content_type: str) -> bytes:
    ext = MIME_TO_EXT[content_type]

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / f"input{ext}"
        output_path = Path(tmpdir) / "input.pdf"
        input_path.write_bytes(content)

        result = _run_command(
            [
                "soffice",
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                tmpdir,
                f"-env:UserInstallation=file://{tmpdir}/profile",
                str(input_path),
            ],
            timeout=120,
            missing_message="LibreOffice is not available in this environment.",
        )

        if result.returncode != 0 or not output_path.exists():
            raise ApiError(
                status_code=500,
                code="conversion_failed",
                message="Falha na conversao do documento.",
                details={"stderr": _trim_process_output(result.stderr)},
            )

        return output_path.read_bytes()


def _convert_image_with_libreoffice(content: bytes, content_type: str) -> bytes:
    ext = IMAGE_MIME_TO_EXT.get(content_type, ".jpg")

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / f"input{ext}"
        output_path = Path(tmpdir) / "input.pdf"
        input_path.write_bytes(content)

        result = _run_command(
            [
                "soffice",
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                tmpdir,
                f"-env:UserInstallation=file://{tmpdir}/profile",
                str(input_path),
            ],
            timeout=120,
            missing_message="LibreOffice is not available in this environment.",
        )

        if result.returncode != 0 or not output_path.exists():
            raise ApiError(
                status_code=500,
                code="conversion_failed",
                message="Falha na conversao da imagem.",
                details={"stderr": _trim_process_output(result.stderr)},
            )

        return output_path.read_bytes()


def _sanitize_image(content: bytes) -> bytes:
    """Re-encode an image to strip ICC profiles and problematic metadata.

    Some ICC profiles (e.g. short lcms "c2" profiles) cause Adobe Acrobat to
    render the resulting PDF as a solid black page.  Re-saving through Pillow
    in sRGB without the original ICC profile eliminates the issue.
    """
    from PIL import Image

    img = Image.open(io.BytesIO(content))
    if not img.info.get("icc_profile"):
        return content  # nothing to strip

    out = io.BytesIO()
    save_kwargs: dict[str, object] = {}
    if img.format == "JPEG" or img.mode in ("RGB", "L"):
        save_kwargs["format"] = "JPEG"
        save_kwargs["quality"] = 95
        if img.mode == "RGBA":
            img = img.convert("RGB")
    else:
        save_kwargs["format"] = img.format or "PNG"
        if img.mode == "RGBA":
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3])
            img = background

    img.save(out, **save_kwargs)
    return out.getvalue()


def convert_to_pdf(content: bytes, content_type: str | None, filename: str | None) -> bytes:
    """Convert a supported office document or image to PDF."""
    resolved_content_type = _resolve_convert_content_type(content_type, filename)
    if resolved_content_type is None:
        raise ApiError(
            status_code=400,
            code="unsupported_media_type",
            message="Formato nao suportado.",
        )

    if resolved_content_type in OFFICE_MIMES:
        return _convert_office(content, resolved_content_type)

    try:
        return img2pdf.convert(_sanitize_image(content))
    except Exception:
        return _convert_image_with_libreoffice(content, resolved_content_type)


def pdf_first_page_to_image(content: bytes, fmt: str) -> tuple[bytes, str, str]:
    """Render the first page of a PDF to PNG or JPEG."""
    import pymupdf

    doc = None
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ApiError(
            status_code=400,
            code="invalid_pdf",
            message="Nao foi possivel abrir o PDF. Verifique se o ficheiro e valido.",
        ) from exc

    try:
        if len(doc) == 0:
            raise ApiError(
                status_code=400,
                code="invalid_pdf",
                message="O PDF nao contem paginas para converter.",
            )

        page = doc[0]
        pix = page.get_pixmap(dpi=300)
        if fmt == "jpeg":
            return pix.tobytes("jpeg", jpg_quality=92), "image/jpeg", "jpg"
        return pix.tobytes("png"), "image/png", "png"
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(
            status_code=500,
            code="conversion_failed",
            message="Nao foi possivel converter este PDF para imagem.",
        ) from exc
    finally:
        if doc is not None:
            doc.close()


def ocr_pdf(content: bytes, language: str) -> bytes:
    """Run OCRmyPDF with the requested language."""
    lang_code = LANGUAGE_MAP.get(language)
    if lang_code is None:
        raise ApiError(
            status_code=400,
            code="unsupported_language",
            message=(
                f"Unsupported language: {language}. "
                f"Supported: {', '.join(LANGUAGE_MAP.keys())}"
            ),
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.pdf"
        output_path = Path(tmpdir) / "output.pdf"
        input_path.write_bytes(content)

        result = _run_command(
            [
                "ocrmypdf",
                "--skip-text",
                "--output-type",
                "pdf",
                "-l",
                lang_code,
                "--deskew",
                "--clean",
                "--optimize",
                "1",
                "--tesseract-timeout",
                "60",
                str(input_path),
                str(output_path),
            ],
            timeout=120,
            missing_message="OCRmyPDF is not available in this environment.",
        )

        if result.returncode == 2:
            raise ApiError(
                status_code=400,
                code="invalid_pdf",
                message="Ficheiro PDF invalido ou corrompido.",
            )

        if result.returncode == 8:
            raise ApiError(
                status_code=400,
                code="password_protected_pdf",
                message="PDF protegido por palavra-passe. Remova a protecao primeiro.",
            )

        if result.returncode != 0 or not output_path.exists():
            raise ApiError(
                status_code=500,
                code="ocr_failed",
                message="Falha no processamento OCR.",
                details={"stderr": _trim_process_output(result.stderr)},
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
                f"Invalid conformance level: {conformance}. "
                f"Supported: {', '.join(CONFORMANCE_MAP.keys())}"
            ),
        )

    pdfa_definition_template = next(Path("/usr/share/ghostscript").rglob("PDFA_def.ps"), None)
    icc_profile_path = Path("/usr/share/color/icc/ghostscript/default_rgb.icc")
    if pdfa_definition_template is None or not icc_profile_path.exists():
        raise ApiError(
            status_code=503,
            code="tool_unavailable",
            message="Ghostscript PDF/A resources are not available in this environment.",
        )

    template_text = pdfa_definition_template.read_text(encoding="latin-1")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_path = tmpdir_path / "input.pdf"
        output_path = tmpdir_path / "output.pdf"
        pdfa_definition_path = tmpdir_path / "PDFA_def.ps"

        input_path.write_bytes(content)
        pdfa_definition_path.write_text(
            template_text.replace("/ICCProfile (srgb.icc)", f"/ICCProfile ({icc_profile_path})"),
            encoding="latin-1",
        )

        result = _run_command(
            [
                "gs",
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
            timeout=120,
            missing_message="Ghostscript is not available in this environment.",
        )

        if result.returncode != 0 or not output_path.exists():
            stderr = _trim_process_output(result.stderr)
            status_code = 400 if "Password" in stderr or "password" in stderr else 500
            error_code = (
                "password_protected_pdf" if status_code == 400 else "pdfa_conversion_failed"
            )
            message = (
                "PDF protegido por palavra-passe. Remova a protecao primeiro."
                if status_code == 400
                else "PDF/A conversion failed."
            )
            raise ApiError(
                status_code=status_code,
                code=error_code,
                message=message,
                details={"stderr": stderr},
            )

        return output_path.read_bytes()


def _open_pikepdf(content: bytes):
    import pikepdf

    try:
        return pikepdf.open(io.BytesIO(content))
    except pikepdf.PasswordError as exc:
        raise ApiError(
            status_code=400,
            code="password_protected_pdf",
            message="Este PDF ja esta protegido com palavra-passe.",
        ) from exc
    except Exception as exc:
        raise ApiError(
            status_code=400,
            code="invalid_pdf",
            message="Nao foi possivel abrir o PDF. Verifique se o ficheiro e valido.",
        ) from exc


def protect_pdf(content: bytes, password: str) -> bytes:
    """Encrypt a PDF with AES-256 permissions."""
    import pikepdf

    if not password or not password.strip():
        raise ApiError(
            status_code=400,
            code="invalid_password",
            message="Password is required.",
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
            message="Failed to protect PDF.",
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
            message="No field values provided.",
        )

    pdf = _open_pikepdf(content)
    if pdf.Root.get("/AcroForm") is None:
        pdf.close()
        raise ApiError(
            status_code=400,
            code="missing_form_fields",
            message="PDF has no form fields.",
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
                message="Some field names do not exist in the uploaded PDF form.",
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
            message="Failed to process form.",
        ) from exc
    finally:
        pdf.close()


def redact_pdf(
    content: bytes,
    *,
    strategy: str,
    custom_text: str = "",
    regex_pattern: str = "",
) -> bytes:
    """Apply text redaction to a PDF."""
    import pymupdf

    if strategy not in VALID_REDACTION_STRATEGIES:
        raise ApiError(
            status_code=400,
            code="invalid_redaction_strategy",
            message="Invalid redaction strategy.",
        )

    if strategy == "custom" and not custom_text.strip():
        raise ApiError(
            status_code=400,
            code="missing_custom_text",
            message="Custom text is required.",
        )

    if strategy == "regex":
        if not regex_pattern.strip():
            raise ApiError(
                status_code=400,
                code="missing_regex_pattern",
                message="Regex pattern is required.",
            )
        if len(regex_pattern) > MAX_REGEX_LENGTH:
            raise ApiError(
                status_code=400,
                code="regex_too_long",
                message=f"Regex pattern too long (max {MAX_REGEX_LENGTH} chars).",
            )
        try:
            re.compile(regex_pattern)
        except re.error as exc:
            raise ApiError(
                status_code=400,
                code="invalid_regex_pattern",
                message="Invalid regex pattern.",
            ) from exc

    if strategy in PATTERNS:
        pattern = PATTERNS[strategy]
        flags = 0
    elif strategy == "custom":
        pattern = re.escape(custom_text)
        flags = re.IGNORECASE
    else:
        pattern = regex_pattern
        flags = 0

    doc = None
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ApiError(
            status_code=400,
            code="invalid_pdf",
            message="Nao foi possivel abrir o PDF. Verifique se o ficheiro e valido.",
        ) from exc

    try:
        with REDACTION_LOCK:
            pymupdf.TOOLS.set_small_glyph_heights(True)
            try:
                for page in doc:
                    text = page.get_text("text")
                    matches = {match.group() for match in re.finditer(pattern, text, flags)}
                    page_has_redactions = False
                    for match_text in matches:
                        rects = page.search_for(match_text)
                        for rect in rects:
                            page.add_redact_annot(rect, fill=(0, 0, 0))
                            page_has_redactions = True

                    if page_has_redactions:
                        page.apply_redactions(
                            images=pymupdf.PDF_REDACT_IMAGE_NONE,
                            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                        )
            finally:
                pymupdf.TOOLS.set_small_glyph_heights(False)

        return doc.tobytes(garbage=4, deflate=True)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(
            status_code=500,
            code="redaction_failed",
            message="Nao foi possivel processar a redacao deste PDF.",
        ) from exc
    finally:
        if doc is not None:
            doc.close()


def build_health_payload() -> dict[str, Any]:
    """Return service health data and dependency versions."""
    try:
        import pymupdf

        pymupdf_version = pymupdf.VersionBind
    except ImportError:
        pymupdf_version = "unavailable"

    try:
        import pikepdf

        pikepdf_version = pikepdf.__version__
    except ImportError:
        pikepdf_version = "unavailable"

    def read_version(command: list[str], timeout: int, *, first_line: bool = False) -> str:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "unavailable"

        if result.returncode != 0:
            return "unavailable"

        output = result.stdout.strip()
        if first_line:
            output = output.splitlines()[0] if output else ""
        return output or "unavailable"

    return {
        "status": "ok",
        "versions": {
            "pymupdf": pymupdf_version,
            "pikepdf": pikepdf_version,
            "ghostscript": read_version(["gs", "--version"], 10),
            "tesseract": read_version(["tesseract", "--version"], 10, first_line=True),
            "libreoffice": read_version(["soffice", "--headless", "--version"], 30),
        },
    }
