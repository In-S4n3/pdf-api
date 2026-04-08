"""TudoPDF PDF Processing API.

FastAPI application factory with lifespan management and error handling.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api_errors import ApiError
from app.config import DEBUG, get_settings
from app.router import router

logger = logging.getLogger(__name__)


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
        logger.warning("API_KEY is not configured; requests are currently unauthenticated.")
    yield


app = FastAPI(
    title="TudoPDF API",
    description="PDF processing API for TudoPDF",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc" if DEBUG else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if DEBUG else [],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(router)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a request id to every response for easier debugging."""
    request.state.request_id = request.headers.get("X-Request-ID") or str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


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


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
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
                message="Request validation failed.",
                details=exc.errors(),
            ),
        )

    return JSONResponse(
        status_code=422,
        content={"error": "Dados invalidos na requisicao"},
    )
