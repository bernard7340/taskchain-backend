"""Alarm REST endpoints.

GET  /api/v1/alarms              → List[AlarmState]
GET  /api/v1/alarms/{id}         → AlarmState
POST /api/v1/alarms/{id}/arm     → AlarmState  (set armed = True)
POST /api/v1/alarms/{id}/disarm  → AlarmState  (set armed = False)
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException, status

from models.alarm import AlarmState
from polling.scheduler import get_cached_appliance
from services.alarm_manager import alarm_manager

router = APIRouter(prefix="/alarms", tags=["alarms"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=List[AlarmState],
    summary="List alarm states for all tracked appliances",
    description=(
        "Returns the current arm state for every appliance that has been discovered "
        "by the polling scheduler."
    ),
)
async def list_alarms() -> List[AlarmState]:
    return alarm_manager.get_all_alarm_states()


@router.get(
    "/{appliance_id}",
    response_model=AlarmState,
    summary="Get alarm state for a specific appliance",
)
async def get_alarm(appliance_id: str) -> AlarmState:
    _assert_appliance_exists(appliance_id)
    return alarm_manager.get_alarm_state(appliance_id)


@router.post(
    "/{appliance_id}/arm",
    response_model=AlarmState,
    summary="Arm the alarm for the current cycle",
    description=(
        "When the appliance's current cycle completes, a high-priority FCM "
        "critical alert will be sent to the registered Android device.  "
        "The arm state resets automatically at the start of the next cycle."
    ),
    status_code=status.HTTP_200_OK,
)
async def arm_alarm(appliance_id: str) -> AlarmState:
    _assert_appliance_exists(appliance_id)
    return await alarm_manager.arm_alarm(appliance_id)


@router.post(
    "/{appliance_id}/disarm",
    response_model=AlarmState,
    summary="Disarm the alarm for the current cycle",
    description=(
        "Reverts to standard-priority push notification on cycle completion.  "
        "The appliance continues to be tracked."
    ),
    status_code=status.HTTP_200_OK,
)
async def disarm_alarm(appliance_id: str) -> AlarmState:
    _assert_appliance_exists(appliance_id)
    return await alarm_manager.disarm_alarm(appliance_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_appliance_exists(appliance_id: str) -> None:
    """Raise 404 if the appliance_id is not yet known to the cache."""
    if get_cached_appliance(appliance_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Appliance '{appliance_id}' not found in the cache.  "
                "It may not have been discovered yet – wait for the next poll cycle."
            ),
        )
