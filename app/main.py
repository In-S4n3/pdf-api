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
from app.config import DEBUG, get_settings
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


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a request id to every response for easier debugging."""
    request.state.request_id = request.headers.get("X-Request-ID") or str(uuid4())
    try:
        response = await call_next(request)
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


# Registered on Starlette's class, not FastAPI's subclass: the router raises the
# base class for an unmatched path or method, and a handler on the subclass let
# those out as Starlette's own `{"detail": "Not Found"}` — outside the v2
# envelope and without the request id.
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Return JSON error per D-07."""
    if _is_v2_request(request):
        message = exc.detail if isinstance(exc.detail, str) else "HTTP request failed."
        return JSONResponse(
            status_code=exc.status_code,
            content=_v2_error_content(
                request,
                code="http_error",
                message=message,
                details=exc.detail if not isinstance(exc.detail, str) else None,
            ),
        )

    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
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
                details=exc.errors(),
            ),
        )

    return JSONResponse(
        status_code=422,
        content={"error": "Dados inválidos na requisição"},
    )
