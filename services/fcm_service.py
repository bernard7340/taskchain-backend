"""Firebase Cloud Messaging service.

Sends two classes of notifications:
  - Critical alarm  : high-priority FCM with vibration pattern, for armed cycles.
  - Standard notice : normal-priority FCM, for unarmed cycle completions.

Multi-user support
------------------
``send_critical_alarm_to`` and ``send_standard_notification_to`` accept an
explicit FCM token so each user's notifications go to their own device.

The legacy ``send_critical_alarm`` and ``send_standard_notification`` functions
that use a single global token are retained for backward compatibility but
should not be used in new code.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import firebase_admin
from firebase_admin import credentials, messaging

from config import get_settings
from models.appliance import Appliance

logger = logging.getLogger(__name__)

_firebase_app: Optional[firebase_admin.App] = None

# Legacy single-device token — updated by /api/v1/fcm-token endpoint.
_fcm_token: str = ""


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def initialize_firebase() -> None:
    """Initialize the Firebase Admin SDK.  Safe to call multiple times.

    Credentials are loaded from (in order of preference):
    1. FIREBASE_CREDENTIALS_JSON  env var  — raw JSON string
    2. FIREBASE_CREDENTIALS_BASE64 env var — base64-encoded JSON (Railway-friendly)
    3. firebase_credentials_path from config — local file path
    """
    global _firebase_app, _fcm_token

    if _firebase_app is not None:
        return

    settings = get_settings()
    _fcm_token = settings.fcm_default_token

    import base64
    import json as _json

    cred = None

    # 1. Raw JSON in env var
    raw_json = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
    if raw_json:
        try:
            cred = credentials.Certificate(_json.loads(raw_json))
            logger.info("Firebase credentials loaded from FIREBASE_CREDENTIALS_JSON.")
        except Exception as exc:
            logger.error("Failed to parse FIREBASE_CREDENTIALS_JSON: %s", exc)

    # 2. Base64-encoded JSON in env var (easier to paste into Railway UI)
    if cred is None:
        b64 = os.environ.get("FIREBASE_CREDENTIALS_BASE64", "").strip()
        if b64:
            try:
                cred = credentials.Certificate(_json.loads(base64.b64decode(b64).decode()))
                logger.info("Firebase credentials loaded from FIREBASE_CREDENTIALS_BASE64.")
            except Exception as exc:
                logger.error("Failed to parse FIREBASE_CREDENTIALS_BASE64: %s", exc)

    # 3. File path
    if cred is None:
        try:
            cred = credentials.Certificate(settings.firebase_credentials_path)
            logger.info("Firebase credentials loaded from file: %s", settings.firebase_credentials_path)
        except Exception as exc:
            logger.warning("Firebase credentials file not found: %s", exc)

    if cred is None:
        logger.error(
            "No Firebase credentials available. Set FIREBASE_CREDENTIALS_JSON, "
            "FIREBASE_CREDENTIALS_BASE64, or place firebase-credentials.json in the backend folder."
        )
        return

    try:
        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin SDK initialised successfully.")
    except Exception as exc:
        logger.error("Failed to initialise Firebase Admin SDK: %s", exc)
        # Keep _firebase_app as None so callers know FCM is unavailable.


def set_fcm_token(token: str) -> None:
    """Update the global FCM device token (legacy single-user path)."""
    global _fcm_token
    _fcm_token = token
    logger.info("FCM device token updated (legacy).")


def get_fcm_token() -> str:
    return _fcm_token


def _is_ready() -> bool:
    return _firebase_app is not None


# ---------------------------------------------------------------------------
# Per-user send helpers (multi-user cloud path)
# ---------------------------------------------------------------------------


async def send_critical_alarm_to(appliance: Appliance, fcm_token: str) -> bool:
    """Send a high-priority critical alert to a specific FCM *fcm_token*.

    Returns True on success, False on failure.
    """
    if not _is_ready():
        logger.warning("FCM not initialised – skipping critical alarm for %s.", appliance.id)
        return False
    if not fcm_token:
        logger.warning("No FCM token for appliance %s – skipping.", appliance.id)
        return False

    message = messaging.Message(
        data={
            "type": "CRITICAL_ALARM",
            "appliance_id": appliance.id,
            "appliance_name": appliance.name,
            "appliance_type": appliance.type,
            "message": f"{appliance.name} is done!",
            "status_detail": appliance.status_detail,
        },
        notification=messaging.Notification(
            title=f"TaskChain – {appliance.name} Done",
            body=f"{appliance.name} has finished its cycle. Tap to review.",
        ),
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id="taskchain_critical",
                priority="max",
                default_vibrate_timings=False,
                vibrate_timings_millis=[0, 500, 200, 500, 200, 500],
                default_sound=True,
            ),
        ),
        token=fcm_token,
    )

    try:
        response = messaging.send(message)
        logger.info("Critical alarm sent for %s. Message ID: %s", appliance.id, response)
        return True
    except messaging.UnregisteredError:
        logger.error("FCM token is unregistered for appliance %s.", appliance.id)
        return False
    except Exception as exc:
        logger.error("Failed to send critical alarm for %s: %s", appliance.id, exc)
        return False


async def send_standard_notification_to(appliance: Appliance, fcm_token: str) -> bool:
    """Send a normal-priority notification to a specific FCM *fcm_token*.

    Returns True on success, False on failure.
    """
    if not _is_ready():
        logger.warning("FCM not initialised – skipping standard notification for %s.", appliance.id)
        return False
    if not fcm_token:
        logger.warning("No FCM token for appliance %s – skipping.", appliance.id)
        return False

    body = appliance.status_detail if appliance.status_detail else f"{appliance.name} has finished."

    message = messaging.Message(
        data={
            "type": "STANDARD_NOTIFICATION",
            "appliance_id": appliance.id,
            "appliance_name": appliance.name,
            "appliance_type": appliance.type,
            "message": f"{appliance.name} is done!",
            "status_detail": appliance.status_detail,
        },
        notification=messaging.Notification(
            title=f"{appliance.name} – Cycle Complete",
            body=body,
        ),
        android=messaging.AndroidConfig(
            priority="normal",
            notification=messaging.AndroidNotification(
                channel_id="taskchain_standard",
                priority="default",
                default_sound=True,
                default_vibrate_timings=True,
            ),
        ),
        token=fcm_token,
    )

    try:
        response = messaging.send(message)
        logger.info("Standard notification sent for %s. Message ID: %s", appliance.id, response)
        return True
    except messaging.UnregisteredError:
        logger.error("FCM token is unregistered for appliance %s.", appliance.id)
        return False
    except Exception as exc:
        logger.error("Failed to send standard notification for %s: %s", appliance.id, exc)
        return False


# ---------------------------------------------------------------------------
# Legacy single-device wrappers (preserved for backward compatibility)
# ---------------------------------------------------------------------------


async def send_critical_alarm(appliance: Appliance) -> bool:
    """Legacy wrapper — sends to the global FCM token."""
    return await send_critical_alarm_to(appliance, _fcm_token)


async def send_standard_notification(appliance: Appliance) -> bool:
    """Legacy wrapper — sends to the global FCM token."""
    return await send_standard_notification_to(appliance, _fcm_token)
