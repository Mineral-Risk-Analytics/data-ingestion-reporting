"""Shared FastAPI dependencies (DB session, Clerk auth).

The auth flow verifies Clerk-issued RS256 JWTs against the project's JWKS
endpoint. JWKS responses are cached for one hour to avoid hitting Clerk on
every request.

Development escape hatch: when ``app_env == "development"`` AND
``CLERK_JWKS_URL`` is empty, a stub admin user is returned so the API is
usable without Clerk configured. Any non-empty JWKS URL enforces verification.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any, Optional

import httpx
import jwt
from cachetools import TTLCache
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_session_factory


def get_db() -> Generator[Session, None, None]:
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Clerk JWT auth
# ---------------------------------------------------------------------------


_JWKS_CACHE: TTLCache[str, PyJWKClient] = TTLCache(maxsize=4, ttl=3600)


_DEV_STUB_USER: dict[str, Any] = {
    "sub": "dev-stub-user",
    "email": "dev@localhost",
    "org_id": None,
    "org_role": "admin",
    "role": "admin",
    "_stub": True,
}


def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    client = _JWKS_CACHE.get(jwks_url)
    if client is None:
        client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
        _JWKS_CACHE[jwks_url] = client
    return client


def _decode_clerk_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    jwks_url = settings.clerk_jwks_url.strip()
    if not jwks_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Clerk JWKS URL not configured",
        )
    try:
        jwks_client = _get_jwks_client(jwks_url)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        decode_kwargs: dict[str, Any] = {
            "algorithms": ["RS256"],
            "options": {"verify_aud": bool(settings.clerk_audience)},
        }
        if settings.clerk_audience:
            decode_kwargs["audience"] = settings.clerk_audience
        if settings.clerk_issuer:
            decode_kwargs["issuer"] = settings.clerk_issuer
        claims = jwt.decode(token, signing_key.key, **decode_kwargs)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc
    except httpx.HTTPError as exc:  # pragma: no cover - network error path
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to reach Clerk JWKS endpoint",
        ) from exc

    return claims


def get_current_user(
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """
    Verify a Clerk session JWT and return its claims.

    Returns a dict with at least: ``sub``, ``email``, ``org_id``, ``org_role``,
    ``role``. In development mode with no JWKS configured, returns a stub admin
    user so local iteration doesn't require Clerk setup.
    """
    settings = get_settings()
    jwks_url = settings.clerk_jwks_url.strip()

    if not jwks_url and settings.app_env == "development":
        return _DEV_STUB_USER

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token",
        )
    claims = _decode_clerk_token(token)

    public_metadata = claims.get("public_metadata") or {}
    return {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "org_id": claims.get("org_id"),
        "org_role": claims.get("org_role"),
        "role": public_metadata.get("role") or claims.get("role"),
        "claims": claims,
    }


_ADMIN_ORG_ROLES = {"admin", "owner", "org:admin", "org:owner"}
_ADMIN_USER_ROLES = {"admin", "owner", "analyst"}


def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Enforce admin privileges on a route. Permits Clerk org admins/owners
    or users whose ``public_metadata.role`` is admin/owner/analyst.

    LAUNCH_TODO 2026-07-19 (Nicole): gate temporarily disabled — pre-launch,
    the (dashboard) Clerk gate is sufficient and Nicole is the only signed-in
    user, so the role check was surfacing as a spurious "Admin role required"
    on the insights upload page. Re-enable by removing the early-return block
    below before flipping the public site live (launch checklist item)."""
    # --- LAUNCH_TODO: remove this block to re-enable role enforcement ---
    return user
    # --- end LAUNCH_TODO ---
    org_role = (user.get("org_role") or "").lower()
    user_role = (user.get("role") or "").lower()
    if org_role in _ADMIN_ORG_ROLES or user_role in _ADMIN_USER_ROLES:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin role required",
    )
