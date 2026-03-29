from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ApplianceType(str, Enum):
    WASHER = "WASHER"
    DRYER = "DRYER"
    OVEN = "OVEN"
    ROBOROCK = "ROBOROCK"


class ApplianceStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    DONE = "DONE"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class Appliance(BaseModel):
    id: str = Field(..., description="Unique appliance identifier")
    name: str = Field(..., description="Human-readable appliance name")
    type: ApplianceType
    status: ApplianceStatus
    status_detail: str = Field(
        default="",
        description="Human-readable status detail, e.g. 'Washing · 28 mins left'",
    )
    minutes_remaining: Optional[int] = Field(
        default=None, description="Estimated minutes until cycle completion"
    )
    is_active: bool = Field(
        default=False,
        description="True when a cycle is actively running",
    )
    last_updated: datetime = Field(default_factory=datetime.utcnow)

    model_config = {"use_enum_values": True}
