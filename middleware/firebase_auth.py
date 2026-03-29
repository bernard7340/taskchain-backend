"""Firebase ID-token authentication middleware / dependency.

In LOCAL_MODE (when Firebase credentials are not configured) all requests
are accepted and assigned the user ID "local_user".  This allows the backend
to run on a PC connected via USB without any cloud setup, while still
producing real SmartHQ appliance data.

When Firebase credentials ARE present, tokens are verified normally.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

logger = logging.getLogger(__name__)

# If no Firebase credentials are configured, run in local / no-auth mode.
_LOCAL_MODE_USER = "local_user"


def _firebase_ready() -> bool:
    """Return True only if WE explicitly initialised Firebase with real credentials."""
    from services import fcm_service
    return fcm_service._firebase_app is not None


async def get_current_user_id(
    authorization: Optional[str] = Header(default=None),
) -> str:
    """Return the caller's user ID.

    - If Firebase is not configured: returns ``"local_user"`` for any request
      so the app works without cloud auth (local USB mode).
    - If Firebase is configured: verifies the Bearer token and returns the UID.
    """
    if not _firebase_ready():
        # Local / dev mode — no auth required.
        logger.debug("Firebase not configured — using local_user.")
        return _LOCAL_MODE_USER

    # Firebase is available — enforce proper auth.
    from firebase_admin import auth as firebase_auth

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
