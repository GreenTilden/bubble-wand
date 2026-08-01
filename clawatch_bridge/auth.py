"""Bearer-token auth dependency (constant-time compare)."""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .config import settings


async def require_token(authorization: str = Header(default="")) -> None:
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(credentials, settings.token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
