"""Appliance REST endpoints.

GET  /api/v1/appliances          → List[Appliance]
GET  /api/v1/appliances/{id}     → Appliance
POST /api/v1/fcm-token           → Register / update FCM device token
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from models.appliance import Appliance
from polling.scheduler import get_all_cached_appliances, get_cached_appliance
from services import fcm_service

router = APIRouter(prefix="/appliances", tags=["appliances"])
fcm_router = APIRouter(tags=["fcm"])


# ---------------------------------------------------------------------------
# Request / response helpers
# ---------------------------------------------------------------------------

class FCMTokenRequest(BaseModel):
    token: str = Field(..., min_length=1, description="Firebase Cloud Messaging device token")


class FCMTokenResponse(BaseModel):
    message: str
    token: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=List[Appliance],
    summary="List all tracked appliances",
    description=(
        "Returns the latest cached state for every appliance the backend is tracking "
        "(SmartHQ GE devices + Roborock vacuums).  The cache is refreshed on the "
        "configured polling interval."
    ),
)
async def list_appliances() -> List[Appliance]:
    return get_all_cached_appliances()


@router.get(
    "/{appliance_id}",
    response_model=Appliance,
    summary="Get a single appliance by ID",
)
async def get_appliance(appliance_id: str) -> Appliance:
    appliance = get_cached_appliance(appliance_id)
    if appliance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Appliance '{appliance_id}' not found. "
                   "It may not have been discovered yet – wait for the next poll cycle.",
        )
    return appliance


@fcm_router.post(
    "/fcm-token",
    response_model=FCMTokenResponse,
    summary="Register or update the Android device FCM token",
    description=(
        "The Android app calls this endpoint on launch (and whenever the FCM token "
        "rotates) so that the backend can direct push notifications to the correct "
        "device."
    ),
    status_code=status.HTTP_200_OK,
)
async def register_fcm_token(body: FCMTokenRequest) -> FCMTokenResponse:
    fcm_service.set_fcm_token(body.token)
    return FCMTokenResponse(message="FCM token registered successfully.", token=body.token)
