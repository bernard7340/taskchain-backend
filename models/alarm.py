from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AlarmState(BaseModel):
    appliance_id: str = Field(..., description="ID of the appliance this alarm tracks")
    is_armed_for_current_cycle: bool = Field(
        default=False,
        description="True if a critical alert should fire when the current cycle ends",
    )
    armed_at: Optional[datetime] = Field(
        default=None, description="UTC timestamp when the alarm was last armed"
    )
    triggered_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp when the alarm last fired (cycle completed while armed)",
    )
