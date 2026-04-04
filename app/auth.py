"""API key authentication middleware.

Scaffold only -- activated in Phase 9 (Cloud Run deployment).
"""


async def verify_api_key():
    """Placeholder for API key verification.

    Phase 9 will implement this as a FastAPI dependency that checks
    the X-API-Key header against config.API_KEY.
    """
    pass
