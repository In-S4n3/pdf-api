"""Version 2 HTTP contract for PDF processing tools."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile

from app.auth import verify_api_key
from app.http_utils import file_response, filename_stem, read_upload_bytes, run_service
from app.services.pdf_tools import (
    build_health_payload,
    compress_pdf,
    convert_pdf_to_pdfa,
    convert_to_pdf,
    fill_form_pdf,
    flatten_pdf,
    ocr_pdf,
    pdf_first_page_to_image,
    protect_pdf,
    redact_pdf,
)
from app.v2_options import (
    EmptyOptions,
    FillFormOptions,
    OcrOptions,
    PdfaOptions,
    PdfToImageOptions,
    ProtectOptions,
    RedactOptions,
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
RedactOptionsDep = Annotated[RedactOptions, options_dependency(RedactOptions)]


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
    result, media_type, ext = await run_service(
        pdf_first_page_to_image,
        content,
        options.format.value,
    )
    return file_response(
        result,
        media_type,
        f"{filename_stem(file.filename)}.{ext}",
        f"output.{ext}",
    )


@router.post("/protect")
async def protect_v2(
    file: UploadedFile,
    options: ProtectOptionsDep,
    _key: ApiKeyDep,
):
    content = await read_upload_bytes(file)
    result = await run_service(protect_pdf, content, options.userPassword)
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
    )
    return file_response(result, "application/pdf", file.filename, "output.pdf")
