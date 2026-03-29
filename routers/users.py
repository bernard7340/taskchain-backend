"""User management REST endpoints.

POST   /api/v1/users/register             → Register user + SmartHQ creds
DELETE /api/v1/users/me                   → Remove user session
PUT    /api/v1/users/me/fcm-token         → Update FCM token
GET    /api/v1/users/me/appliances        → Live appliance list for this user
POST   /api/v1/users/me/alarms/{id}/arm   → Arm alarm for this user's appliance
POST   /api/v1/users/me/alarms/{id}/disarm → Disarm alarm

All endpoints require a valid Firebase Bearer token in the Authorization header.
The Firebase UID extracted from that token is the user identifier throughout.
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from middleware.firebase_auth import get_current_user_id
from models.alarm import AlarmState
from models.appliance import Appliance
from services.user_session_manager import user_session_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    smarthq_username: str = Field(..., description="SmartHQ account email address")
    smarthq_password: str = Field(..., min_length=1, description="SmartHQ account password")
    fcm_token: str = Field(default="pending", description="Firebase Cloud Messaging device token")

    @field_validator("smarthq_username")
    @classmethod
    def username_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("smarthq_username must not be empty")
        return v


class RegisterWithCodeRequest(BaseModel):
    auth_code: str = Field(..., min_length=1, description="GE OAuth2 authorization code from WebView login")
    fcm_token: str = Field(default="pending", description="Firebase Cloud Messaging device token")

    @field_validator("auth_code")
    @classmethod
    def code_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("auth_code must not be empty")
        return v


class RegisterResponse(BaseModel):
    user_id: str
    appliances: List[Appliance]
    message: str = "User registered successfully."


class FcmTokenRequest(BaseModel):
    fcm_token: str = Field(..., min_length=1, description="New Firebase Cloud Messaging token")


class FcmTokenResponse(BaseModel):
    message: str
    user_id: str


class DeleteResponse(BaseModel):
    message: str
    user_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _require_session(user_id: str):
    """Return the UserSession or raise 404."""
    session = await user_session_manager.get_session(user_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No active session for user '{user_id}'. "
                "Call POST /api/v1/users/register first."
            ),
        )
    return session


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a user and connect their SmartHQ account",
    description=(
        "Accepts SmartHQ credentials and an FCM device token.  "
        "Creates an isolated per-user session, authenticates with SmartHQ, "
        "performs an initial appliance fetch, and starts background polling.  "
        "Calling this again for the same user replaces the existing session."
    ),
)
async def register_user(
    body: RegisterRequest,
    user_id: str = Depends(get_current_user_id),
) -> RegisterResponse:
    logger.info("Registering user=%s with SmartHQ username=%s", user_id, body.smarthq_username)

    try:
        session = await user_session_manager.register_user(
            user_id=user_id,
            smarthq_username=body.smarthq_username,
            smarthq_password=body.smarthq_password,
            fcm_token=body.fcm_token,
        )
    except Exception as exc:
        logger.error("Failed to register user=%s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not connect to SmartHQ: {exc}",
        )

    appliances = list(session.appliances.values())
    return RegisterResponse(user_id=user_id, appliances=appliances)


@router.post(
    "/register-with-code",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a user via GE OAuth2 authorization code (WebView login)",
    description=(
        "The Android app loads GE's OAuth2 login page in a WebView on the phone "
        "(not blocked by CAPTCHA).  After the user authenticates, GE redirects to "
        "the redirect URI containing an authorization code.  The app sends that code "
        "here; the backend exchanges it for access+refresh tokens and opens the "
        "SmartHQ WebSocket connection.  No username/password is stored."
    ),
)
async def register_user_with_code(
    body: RegisterWithCodeRequest,
    user_id: str = Depends(get_current_user_id),
) -> RegisterResponse:
    logger.info("Registering user=%s via OAuth2 code", user_id)

    try:
        session = await user_session_manager.register_user_with_code(
            user_id=user_id,
            auth_code=body.auth_code,
            fcm_token=body.fcm_token,
        )
    except Exception as exc:
        logger.error("Failed to register user=%s via code: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not connect to SmartHQ: {exc}",
        )

    appliances = list(session.appliances.values())
    return RegisterResponse(
        user_id=user_id,
        appliances=appliances,
        message="Connected via OAuth2 login.",
    )


@router.delete(
    "/me",
    response_model=DeleteResponse,
    status_code=status.HTTP_200_OK,
    summary="Remove this user's session",
    description=(
        "Stops background polling and removes all in-memory state for the "
        "authenticated user.  Their SmartHQ credentials are discarded."
    ),
)
async def unregister_user(
    user_id: str = Depends(get_current_user_id),
) -> DeleteResponse:
    removed = await user_session_manager.unregister_user(user_id)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active session found for user '{user_id}'.",
        )
    return DeleteResponse(message="Session removed successfully.", user_id=user_id)


@router.put(
    "/me/fcm-token",
    response_model=FcmTokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Update the FCM device token for this user",
    description=(
        "Call this whenever the Android app receives a new FCM token "
        "(FirebaseMessagingService.onNewToken callback)."
    ),
)
async def update_fcm_token(
    body: FcmTokenRequest,
    user_id: str = Depends(get_current_user_id),
) -> FcmTokenResponse:
    updated = await user_session_manager.update_fcm_token(user_id, body.fcm_token)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active session for user '{user_id}'. Register first.",
        )
    return FcmTokenResponse(message="FCM token updated.", user_id=user_id)


@router.get(
    "/me/appliances",
    response_model=List[Appliance],
    status_code=status.HTTP_200_OK,
    summary="Get this user's appliances with live status",
    description=(
        "Returns the latest cached appliance state for the authenticated user.  "
        "The cache is updated every polling cycle (every 30 s by default)."
    ),
)
async def get_appliances(
    user_id: str = Depends(get_current_user_id),
) -> List[Appliance]:
    session = await _require_session(user_id)
    return list(session.appliances.values())


@router.get(
    "/me/appliances/{appliance_id}",
    response_model=Appliance,
    status_code=status.HTTP_200_OK,
    summary="Get a single appliance for this user",
)
async def get_appliance(
    appliance_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Appliance:
    session = await _require_session(user_id)
    appliance = session.appliances.get(appliance_id)
    if appliance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Appliance '{appliance_id}' not found for user '{user_id}'.",
        )
    return appliance


@router.get(
    "/me/alarms",
    response_model=List[AlarmState],
    status_code=status.HTTP_200_OK,
    summary="List alarm states for all of this user's appliances",
)
async def list_alarms(
    user_id: str = Depends(get_current_user_id),
) -> List[AlarmState]:
    session = await _require_session(user_id)
    return list(session.alarm_states.values())


@router.post(
    "/me/alarms/{appliance_id}/arm",
    response_model=AlarmState,
    status_code=status.HTTP_200_OK,
    summary="Arm the alarm for this user's appliance",
    description=(
        "When the appliance's current cycle completes, a high-priority FCM "
        "critical alert will fire on this user's registered device.  "
        "The arm state resets automatically at the start of the next cycle."
    ),
)
async def arm_alarm(
    appliance_id: str,
    user_id: str = Depends(get_current_user_id),
) -> AlarmState:
    session = await _require_session(user_id)
    if appliance_id not in session.appliances:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Appliance '{appliance_id}' not found for user '{user_id}'.",
        )
    return await session.arm_alarm(appliance_id)


@router.post(
    "/me/alarms/{appliance_id}/disarm",
    response_model=AlarmState,
    status_code=status.HTTP_200_OK,
    summary="Disarm the alarm for this user's appliance",
    description=(
        "Reverts to a standard push notification on cycle completion.  "
        "The appliance continues to be tracked and polled."
    ),
)
async def disarm_alarm(
    appliance_id: str,
    user_id: str = Depends(get_current_user_id),
) -> AlarmState:
    session = await _require_session(user_id)
    if appliance_id not in session.appliances:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Appliance '{appliance_id}' not found for user '{user_id}'.",
        )
    return await session.disarm_alarm(appliance_id)
