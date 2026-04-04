"""API key authentication.

Health endpoint does NOT use this dependency (Cloud Run health checks need unauthenticated access).
All other endpoints (echo, future tool endpoints) use Depends(verify_api_key).
"""

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from app.config import API_KEY

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    api_key: str | None = Security(_api_key_header),
) -> str:
    """Validate X-API-Key header against configured API_KEY.

    Returns the validated key on success.
    Raises 401 if missing or invalid.
    When API_KEY is empty (local dev), allows all requests.
    """
    if not API_KEY:
        return ""
    if not api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header missing")
    if api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key
