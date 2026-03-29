"""Webhook endpoints.

POST /api/v1/webhook/smarthq
    Receives push updates from SmartHQ (if the SmartHQ platform supports
    outbound webhooks / push notifications to a registered callback URL).
    This allows near-real-time state changes without waiting for the next
    scheduled poll.

The handler parses the incoming payload, builds an Appliance object, diffs it
against the cache, and triggers the alarm manager — exactly the same path used
by the polling scheduler.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from models.appliance import Appliance, ApplianceStatus, ApplianceType
from polling.scheduler import get_cached_appliance, upsert_appliance
from services.alarm_manager import alarm_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhooks"])


# ---------------------------------------------------------------------------
# SmartHQ webhook payload schema
# SmartHQ may vary; this covers the most common push-notification envelope.
# ---------------------------------------------------------------------------

class SmartHQWebhookAttributes(BaseModel):
    applianceState: Optional[str] = None
    operationMode: Optional[str] = None
    cycleName: Optional[str] = None
    selectedCycle: Optional[str] = None
    timeRemaining: Optional[int] = None
    remainingTime: Optional[int] = None
    faultCode: Optional[str] = None
    errorCode: Optional[str] = None

    model_config = {"extra": "allow"}


class SmartHQWebhookPayload(BaseModel):
    applianceId: Optional[str] = Field(default=None)
    id: Optional[str] = Field(default=None)
    nickname: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    modelNumber: Optional[str] = Field(default=None)
    type: Optional[str] = Field(default=None)
    applianceType: Optional[str] = Field(default=None)
    applianceState: Optional[str] = Field(default=None)
    operationMode: Optional[str] = Field(default=None)
    attributes: Optional[SmartHQWebhookAttributes] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Status / type mapping helpers (mirrors smarthq_service.py)
# ---------------------------------------------------------------------------

_STATUS_MAP: Dict[str, ApplianceStatus] = {
    "IDLE": ApplianceStatus.IDLE,
    "STANDBY": ApplianceStatus.IDLE,
    "OFF": ApplianceStatus.IDLE,
    "POWERED_OFF": ApplianceStatus.IDLE,
    "RUNNING": ApplianceStatus.RUNNING,
    "IN_USE": ApplianceStatus.RUNNING,
    "ACTIVE": ApplianceStatus.RUNNING,
    "SENSING": ApplianceStatus.RUNNING,
    "DELAY_START": ApplianceStatus.RUNNING,
    "PAUSED": ApplianceStatus.RUNNING,
    "END_OF_CYCLE": ApplianceStatus.DONE,
    "COMPLETE": ApplianceStatus.DONE,
    "DONE": ApplianceStatus.DONE,
    "FINISHED": ApplianceStatus.DONE,
    "FAULT": ApplianceStatus.ERROR,
    "ERROR": ApplianceStatus.ERROR,
}

_TYPE_MAP: Dict[str, ApplianceType] = {
    "washer": ApplianceType.WASHER,
    "dryer": ApplianceType.DRYER,
    "oven": ApplianceType.OVEN,
    "range": ApplianceType.OVEN,
    "dishwasher": ApplianceType.WASHER,
}


def _map_status(raw: str) -> ApplianceStatus:
    return _STATUS_MAP.get(raw.upper(), ApplianceStatus.UNKNOWN)


def _map_type(raw: str) -> ApplianceType:
    return _TYPE_MAP.get(raw.lower(), ApplianceType.WASHER)


def _build_appliance_from_payload(payload: SmartHQWebhookPayload) -> Appliance:
    appliance_id = payload.applianceId or payload.id or "unknown"
    name = payload.nickname or payload.name or payload.modelNumber or appliance_id
    raw_type = payload.type or payload.applianceType or "washer"
    raw_status = payload.applianceState or payload.operationMode or "UNKNOWN"

    attrs = payload.attributes or SmartHQWebhookAttributes()
    time_remaining: Optional[int] = attrs.timeRemaining or attrs.remainingTime

    status = _map_status(raw_status)

    # Build status_detail
    cycle_name = attrs.cycleName or attrs.selectedCycle or ""
    detail_parts = []
    if status == ApplianceStatus.RUNNING:
        if cycle_name:
            detail_parts.append(cycle_name.replace("_", " ").title())
        if time_remaining is not None:
            detail_parts.append(f"{time_remaining} mins left")
        status_detail = " · ".join(detail_parts) if detail_parts else "Running"
    elif status == ApplianceStatus.DONE:
        status_detail = "Cycle complete"
    elif status == ApplianceStatus.IDLE:
        status_detail = "Idle"
    elif status == ApplianceStatus.ERROR:
        fault = attrs.faultCode or attrs.errorCode or "Unknown error"
        status_detail = f"Error: {fault}"
    else:
        status_detail = "Unknown"

    return Appliance(
        id=appliance_id,
        name=name,
        type=_map_type(raw_type),
        status=status,
        status_detail=status_detail,
        minutes_remaining=int(time_remaining) if time_remaining is not None else None,
        is_active=(status == ApplianceStatus.RUNNING),
        last_updated=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/smarthq",
    summary="Receive SmartHQ push update",
    description=(
        "SmartHQ calls this endpoint when an appliance state changes (if webhook "
        "delivery is configured in the SmartHQ developer portal).  The update is "
        "applied immediately without waiting for the next scheduled poll."
    ),
    status_code=status.HTTP_200_OK,
)
async def smarthq_webhook(request: Request) -> Dict[str, Any]:
    """Handle an inbound SmartHQ webhook event."""
    try:
        raw_body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body.",
        )

    logger.info("SmartHQ webhook received: %s", raw_body)

    # SmartHQ may wrap the payload in an envelope – unwrap if needed.
    appliance_data: Dict[str, Any] = raw_body
    if "appliance" in raw_body and isinstance(raw_body["appliance"], dict):
        appliance_data = raw_body["appliance"]
    elif "data" in raw_body and isinstance(raw_body["data"], dict):
        appliance_data = raw_body["data"]

    try:
        payload = SmartHQWebhookPayload(**appliance_data)
        new_appliance = _build_appliance_from_payload(payload)
    except Exception as exc:
        logger.error("Failed to parse SmartHQ webhook payload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Could not parse appliance data: {exc}",
        )

    # Diff against cache and trigger alarm logic.
    previous = get_cached_appliance(new_appliance.id)
    upsert_appliance(new_appliance)

    if previous is not None:
        previous_status = ApplianceStatus(previous.status)
        new_status = ApplianceStatus(new_appliance.status)
        if previous_status != new_status:
            logger.info(
                "Webhook: '%s' status %s → %s",
                new_appliance.name,
                previous_status.value,
                new_status.value,
            )
            await alarm_manager.on_appliance_status_change(new_appliance, previous_status)
    else:
        # First time we learn about this appliance via webhook.
        alarm_manager.get_alarm_state(new_appliance.id)
        logger.info(
            "Webhook: discovered new appliance '%s' with status %s.",
            new_appliance.name,
            new_appliance.status,
        )

    return {
        "status": "accepted",
        "appliance_id": new_appliance.id,
        "new_status": new_appliance.status,
    }
