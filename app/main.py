"""TudoPDF PDF Processing API.

FastAPI application factory with lifespan management and error handling.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api_errors import ApiError
from app.auth import check_api_key
from app.config import DEBUG, get_settings
from app.http_utils import upload_too_large_message
from app.router import router

logger = logging.getLogger(__name__)

FILL_FORM_DEPRECATED_AT = "@1787875200"  # 2026-08-28T00:00:00Z, RFC 9745 date.
FILL_FORM_DEPRECATION_LINK = (
    '<https://github.com/In-S4n3/pdf-api/blob/main/docs/frontend-v2-migration.md>; '
    'rel="deprecation"'
)


def _is_v2_request(request: Request) -> bool:
    return request.url.path.startswith("/v2/")


def _v2_error_content(
    request: Request,
    *,
    code: str,
    message: str,
    details=None,
):
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "requestId": getattr(request.state, "request_id", None),
        }
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle."""
    settings = get_settings()
    if settings.environment == "production" and not settings.api_key:
        logger.error("API_KEY is not configured; protected routes will fail closed.")
    yield


_settings = get_settings()

app = FastAPI(
    title="TudoPDF API",
    description="PDF processing API for TudoPDF",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if DEBUG else None,
    redoc_url="/redoc" if DEBUG else None,
    openapi_url="/openapi.json" if DEBUG else None,
)


# Room for the multipart framing and the `options` field (Starlette caps a
# non-file part at 1 MiB) on top of the file itself.
MULTIPART_ALLOWANCE = 1024 * 1024 + 64 * 1024


def _presented_api_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key:
        return key
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def _reject_before_body(request: Request):
    """Answer auth failures and oversized uploads before the body is read.

    FastAPI parses the multipart form — spooling the whole upload into
    RAM-backed /tmp — before it runs any dependency, so a 25 MiB request
    without a key was read in full just to be told 401, and one with a key
    to be told 413. The dependency stays as defence in depth.
    """
    if request.method != "POST":
        return None
    try:
        check_api_key(_presented_api_key(request))
    except StarletteHTTPException as exc:
        return await http_exception_handler(request, exc)
    declared = request.headers.get("content-length", "")
    max_bytes = get_settings().max_upload_bytes
    if declared.isdigit() and int(declared) > max_bytes + MULTIPART_ALLOWANCE:
        return await api_error_handler(
            request,
            ApiError(413, "file_too_large", upload_too_large_message(max_bytes)),
        )
    return None


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a request id to every response for easier debugging."""
    request.state.request_id = request.headers.get("X-Request-ID") or str(uuid4())
    try:
        response = await _reject_before_body(request) or await call_next(request)
    except Exception:
        logger.exception(
            "Unhandled request failure (request_id=%s)",
            request.state.request_id,
        )
        if _is_v2_request(request):
            response = JSONResponse(
                status_code=500,
                content=_v2_error_content(
                    request,
                    code="internal_error",
                    message="Erro interno do servidor.",
                ),
            )
        else:
            response = JSONResponse(
                status_code=500,
                content={"error": "Erro interno do servidor."},
            )
    response.headers["X-Request-ID"] = request.state.request_id
    if request.url.path == "/v2/fill-form":
        response.headers["Deprecation"] = FILL_FORM_DEPRECATED_AT
        response.headers["Link"] = FILL_FORM_DEPRECATION_LINK
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if DEBUG else list(_settings.cors_allowed_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(router)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    """Return version-appropriate JSON for domain errors."""
    if _is_v2_request(request):
        return JSONResponse(
            status_code=exc.status_code,
            content=_v2_error_content(
                request,
                code=exc.code,
                message=exc.message,
                details=exc.details,
            ),
        )

    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message},
    )


_V2_HTTP_MESSAGES = {404: "Endereço não encontrado.", 405: "Método não permitido."}


# Registered on Starlette's class, not FastAPI's subclass: the router raises the
# base class for an unmatched path or method, and a handler on the subclass let
# those out as Starlette's own `{"detail": "Not Found"}` — outside the v2
# envelope and without the request id.
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Return JSON error per D-07."""
    # Starlette's 405 carries `Allow`; the default handler forwarded it and
    # this one must too.
    headers = getattr(exc, "headers", None)
    if _is_v2_request(request):
        if exc.status_code == 400:
            # Starlette's multipart parser: "Part exceeded maximum size of
            # 1024KB." and friends. A malformed request, not a server fault.
            return JSONResponse(
                status_code=400,
                content=_v2_error_content(
                    request,
                    code="invalid_request",
                    message=(
                        "Não foi possível ler o pedido (formulário inválido ou demasiado grande)."
                    ),
                ),
                headers=headers,
            )
        message = _V2_HTTP_MESSAGES.get(exc.status_code) or (
            exc.detail if isinstance(exc.detail, str) else "HTTP request failed."
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_v2_error_content(
                request,
                code="http_error",
                message=message,
                details=exc.detail if not isinstance(exc.detail, str) else None,
            ),
            headers=headers,
        )

    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return Portuguese validation error per D-07."""
    if _is_v2_request(request):
        return JSONResponse(
            status_code=422,
            content=_v2_error_content(
                request,
                code="invalid_request",
                message="O pedido não passou a validação.",
                # type/loc/msg only: no echoed input, no pydantic doc URLs.
                details=[
                    {key: error[key] for key in ("type", "loc", "msg") if key in error}
                    for error in exc.errors()
                ],
            ),
        )

    return JSONResponse(
        status_code=422,
        content={"error": "Dados inválidos na requisição"},
    )
