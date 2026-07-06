"""Version 2 HTTP contract for PDF processing tools."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile

from app.api_errors import ApiError
from app.auth import verify_api_key
from app.http_utils import file_response, filename_stem, read_upload_bytes, run_service
from app.services.pdf_tools import (
    _extract_matches,
    build_health_payload,
    compress_pdf,
    convert_pdf_to_pdfa,
    convert_to_pdf,
    fill_form_pdf,
    flatten_pdf,
    ocr_pdf,
    pdf_first_page_to_image,
    pdf_to_docx,
    pdf_to_images,
    pdf_to_xlsx,
    protect_pdf,
    redact_pdf,
    unlock_pdf,
)
from app.v2_options import (
    EmptyOptions,
    FillFormOptions,
    OcrOptions,
    PageSelection,
    PdfaOptions,
    PdfToImageOptions,
    ProtectOptions,
    RedactOptions,
    RedactPreviewOptions,
    UnlockOptions,
    options_dependency,
)

router = APIRouter(prefix="/v2")
ApiKeyDep = Annotated[str, Depends(verify_api_key)]
UploadedFile = Annotated[UploadFile, File(...)]
EmptyOptionsDep = Annotated[EmptyOptions, options_dependency(EmptyOptions)]
FillFormOptionsDep = Annotated[FillFormOptions, options_dependency(FillFormOptions)]
OcrOptionsDep = Annotated[OcrOptions, options_dependency(OcrOptions)]
PdfaOptionsDep = Annotated[PdfaOptions, options_dependency(PdfaOptions)]
PdfToImageOptionsDep = Annotated[PdfToImageOptions, options_dependency(PdfToImageOptions)]
ProtectOptionsDep = Annotated[ProtectOptions, options_dependency(ProtectOptions)]
UnlockOptionsDep = Annotated[UnlockOptions, options_dependency(UnlockOptions)]
RedactOptionsDep = Annotated[RedactOptions, options_dependency(RedactOptions)]
RedactPreviewOptionsDep = Annotated[
    RedactPreviewOptions, options_dependency(RedactPreviewOptions)
]

# Caps payload size on pathological inputs (e.g. a regex that matches every
# word on every page). Frontend uses `truncated` to surface a "refine pattern"
# nudge instead of silently dropping matches.
_PREVIEW_MATCH_CAP = 5000


@router.get("/health")
async def health_v2():
    payload = await run_service(build_health_payload)
    return payload


@router.post("/echo")
async def echo_v2(
    file: UploadedFile,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    filename = file.filename or "output.pdf"
    media_type = file.content_type or "application/pdf"
    return file_response(content, media_type, filename, "output.pdf")


@router.post("/compress")
async def compress_v2(
    file: UploadedFile,
    _options: EmptyOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    result = await run_service(compress_pdf, content)
    return file_response(result, "application/pdf", file.filename, "output.pdf")


@router.post("/convert")
async def convert_v2(
    file: UploadedFile,
    _options: EmptyOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    result = await run_service(convert_to_pdf, content, file.content_type, file.filename)
    return file_response(
        result,
        "application/pdf",
        f"{filename_stem(file.filename)}.pdf",
        "output.pdf",
    )


@router.post("/pdf-to-word")
async def pdf_to_word_v2(
    file: UploadedFile,
    _options: EmptyOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    result = await run_service(pdf_to_docx, content)
    return file_response(
        result,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{filename_stem(file.filename)}.docx",
        "output.docx",
    )


@router.post("/pdf-to-excel")
async def pdf_to_excel_v2(
    file: UploadedFile,
    _options: EmptyOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    result = await run_service(pdf_to_xlsx, content)
    return file_response(
        result,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        f"{filename_stem(file.filename)}.xlsx",
        "output.xlsx",
    )


@router.post("/flatten")
async def flatten_v2(
    file: UploadedFile,
    _options: EmptyOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    result = await run_service(flatten_pdf, content)
    return file_response(result, "application/pdf", file.filename, "output.pdf")


@router.post("/fill-form")
async def fill_form_v2(
    file: UploadedFile,
    options: FillFormOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    result = await run_service(
        fill_form_pdf,
        content,
        options.fields,
        strict_unknown_fields=True,
    )
    return file_response(result, "application/pdf", file.filename, "output.pdf")


@router.post("/ocr")
async def ocr_v2(
    file: UploadedFile,
    options: OcrOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    result = await run_service(ocr_pdf, content, options.language.value)
    return file_response(result, "application/pdf", file.filename, "output.pdf")


@router.post("/pdfa")
async def pdfa_v2(
    file: UploadedFile,
    options: PdfaOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    result = await run_service(convert_pdf_to_pdfa, content, options.conformance.value)
    return file_response(result, "application/pdf", file.filename, "output.pdf")


@router.post("/pdf-to-image")
async def pdf_to_image_v2(
    file: UploadedFile,
    options: PdfToImageOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    if options.pages == PageSelection.all:
        result, media_type, ext = await run_service(
            pdf_to_images, content, options.format.value,
        )
    else:
        result, media_type, ext = await run_service(
            pdf_first_page_to_image, content, options.format.value,
        )
    stem = filename_stem(file.filename)
    fname = f"{stem}-imagens.zip" if ext == "zip" else f"{stem}.{ext}"
    return file_response(result, media_type, fname, f"output.{ext}")


@router.post("/protect")
async def protect_v2(
    file: UploadedFile,
    options: ProtectOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    result = await run_service(protect_pdf, content, options.userPassword)
    return file_response(result, "application/pdf", file.filename, "output.pdf")


@router.post("/pdf-unlock")
async def pdf_unlock_v2(
    file: UploadedFile,
    options: UnlockOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    result = await run_service(unlock_pdf, content, options.password)
    return file_response(result, "application/pdf", file.filename, "output.pdf")


@router.post("/redact")
async def redact_v2(
    file: UploadedFile,
    options: RedactOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    result = await run_service(
        redact_pdf,
        content,
        strategy=options.strategy.value,
        custom_text=options.customText,
        regex_pattern=options.regexPattern,
        confirmed_ids=options.confirmed_ids,
    )
    return file_response(result, "application/pdf", file.filename, "output.pdf")


def _extract_matches_json(
    content: bytes, *, strategy: str, custom_text: str, regex_pattern: str
) -> list[dict[str, object]]:
    """Open the PDF, extract matches via the shared helper, serialise to JSON-ready dicts.

    Lives in the route module (not pdf_tools) because it is purely a transport
    concern — the service helper deliberately returns the dataclass, not JSON.
    """
    import pymupdf

    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ApiError(
            400,
            "invalid_pdf",
            "Não foi possível abrir o PDF. Verifique se o ficheiro é válido.",
        ) from exc

    try:
        # _extract_matches raises 400 password_protected_pdf / invalid_regex_pattern
        # / regex_too_slow as ApiError — middleware turns those into the v2 envelope.
        matches = _extract_matches(
            doc,
            strategy=strategy,
            custom_text=custom_text,
            regex_pattern=regex_pattern,
        )
        return [
            {
                "id": m.id,
                "page": m.page,
                "bbox": list(m.bbox),
                "kind": m.kind,
                "context": m.context,
                "fullMatch": m.full_match,
            }
            for m in matches
        ]
    finally:
        doc.close()


@router.post("/redact/preview")
async def redact_preview_v2(
    file: UploadedFile,
    options: RedactPreviewOptionsDep,
    _key: ApiKeyDep,
):
    """Dry-run: return matches as JSON so the UI can render bbox overlays
    before the user confirms which IDs to apply via POST /v2/redact."""
    content = await read_upload_bytes(file)
    matches_json = await run_service(
        _extract_matches_json,
        content,
        strategy=options.strategy.value,
        custom_text=options.customText,
        regex_pattern=options.regexPattern,
    )

    truncated = len(matches_json) > _PREVIEW_MATCH_CAP
    return {
        "matches": matches_json[:_PREVIEW_MATCH_CAP],
        "total": len(matches_json),
        "truncated": truncated,
    }
