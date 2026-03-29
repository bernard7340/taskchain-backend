"""Firebase ID-token authentication middleware / dependency.

Usage in FastAPI routes:
    from middleware.firebase_auth import get_current_user_id

    @router.get("/protected")
    async def protected(user_id: str = Depends(get_current_user_id)):
        ...

The dependency extracts the Firebase ID token from the ``Authorization: Bearer
<token>`` header, verifies it with the Firebase Admin SDK, and returns the
authenticated ``uid``.  A 401 is raised when:
  - The header is missing or malformed.
  - The token is expired, revoked, or otherwise invalid.
  - The Firebase Admin SDK is not initialised.
"""

from __future__ import annotations

import logging
from typing import Optional

import firebase_admin
from firebase_admin import auth as firebase_auth
from fastapi import Depends, Header, HTTPException, status

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


async def get_current_user_id(
    authorization: Optional[str] = Header(default=None),
) -> str:
    """FastAPI dependency that returns the Firebase UID for the caller.

    Raises ``HTTP 401`` for any authentication failure.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be 'Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    id_token = parts[1].strip()
    if not id_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token is empty.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Ensure Firebase Admin SDK is ready.
    try:
        firebase_admin.get_app()
    except ValueError:
        logger.error("Firebase Admin SDK is not initialised — cannot verify token.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is not available.",
        )

    try:
        decoded = firebase_auth.verify_id_token(id_token)
        uid: str = decoded["uid"]
        logger.debug("Authenticated Firebase UID: %s", uid)
        return uid
    except firebase_auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firebase ID token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except firebase_auth.RevokedIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firebase ID token has been revoked.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except firebase_auth.InvalidIdTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid Firebase ID token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as exc:
        logger.error("Unexpected error verifying Firebase token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not verify authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
