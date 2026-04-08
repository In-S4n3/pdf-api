"""API key authentication.

Health endpoint does NOT use this dependency (Cloud Run health checks need unauthenticated access).
All other endpoints (echo, future tool endpoints) use Depends(verify_api_key).
"""

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
    When API_KEY is empty (local dev), allows all requests.
    """
    settings = get_settings()
    configured_api_key = settings.api_key

    if not configured_api_key:
        if settings.strict_api_key:
            raise HTTPException(status_code=503, detail="API key is not configured")
        return ""

    provided_api_key = api_key or (bearer.credentials if bearer is not None else None)
    if not provided_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header missing")
    if provided_api_key != configured_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return provided_api_key
