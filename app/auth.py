"""API key authentication.

Health endpoint does NOT use this dependency (Cloud Run health checks need unauthenticated access).
All other endpoints (echo, future tool endpoints) use Depends(verify_api_key).
"""

import secrets
from typing import Annotated

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer_scheme = HTTPBearer(auto_error=False)
ApiKeyHeaderDep = Annotated[str | None, Security(_api_key_header)]
BearerTokenDep = Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)]


async def verify_api_key(
    api_key: ApiKeyHeaderDep,
    bearer: BearerTokenDep,
) -> str:
    """Validate X-API-Key header against configured API_KEY.

    Returns the validated key on success.
    Raises 401 if missing or invalid.
    When API_KEY is empty in development, allows all requests. Production and
    explicit strict mode fail closed.
    """
    provided_api_key = api_key or (bearer.credentials if bearer is not None else None)
    check_api_key(provided_api_key)
    return provided_api_key or ""


def check_api_key(provided_api_key: str | None) -> None:
    """Raise 401/503 unless the credential is acceptable.

    Shared by the route dependency and the middleware, which runs it before the
    multipart body is read: FastAPI parses (and spools) the whole upload before
    any dependency, so an unauthenticated caller could fill the only slot.
    """
    settings = get_settings()
    configured_api_key = settings.api_key

    if not configured_api_key:
        if settings.strict_api_key:
            raise HTTPException(status_code=503, detail="API key is not configured")
        return

    if not provided_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header missing")
    if not secrets.compare_digest(provided_api_key.encode(), configured_api_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")
