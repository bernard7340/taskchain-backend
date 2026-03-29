"""Alarm Manager – the core selective-alarm logic for TaskChain.

Each appliance tracked by the system has an associated AlarmState.
The alarm is armed per-cycle: arming while a cycle is running (or before
one starts) means that when the cycle completes a critical FCM alert fires.
If the alarm is not armed, a standard low-priority notification is sent instead.

Arm state resets automatically when a new cycle begins so the user gets a
clean slate for every load.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict

from models.alarm import AlarmState
from models.appliance import Appliance, ApplianceStatus
from services import fcm_service

logger = logging.getLogger(__name__)


class AlarmManager:
    def __init__(self) -> None:
        # Keyed by appliance_id.  Entries are created on first access.
        self.alarm_states: Dict[str, AlarmState] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_state(self, appliance_id: str) -> AlarmState:
        """Return the AlarmState for *appliance_id*, creating it if needed."""
        if appliance_id not in self.alarm_states:
            self.alarm_states[appliance_id] = AlarmState(appliance_id=appliance_id)
        return self.alarm_states[appliance_id]

    # ------------------------------------------------------------------
    # Public API used by the routers
    # ------------------------------------------------------------------

    async def arm_alarm(self, appliance_id: str) -> AlarmState:
        """Arm the alarm for *appliance_id*'s current cycle.

        Arming means a critical FCM alert will fire when the cycle ends.
        """
        state = self._get_or_create_state(appliance_id)
        state.is_armed_for_current_cycle = True
        state.armed_at = datetime.utcnow()
        logger.info("Alarm ARMED for appliance '%s'.", appliance_id)
        return state

    async def disarm_alarm(self, appliance_id: str) -> AlarmState:
        """Disarm the alarm for *appliance_id*'s current cycle.

        A standard notification will still fire on completion.
        """
        state = self._get_or_create_state(appliance_id)
        state.is_armed_for_current_cycle = False
        logger.info("Alarm DISARMED for appliance '%s'.", appliance_id)
        return state

    def get_alarm_state(self, appliance_id: str) -> AlarmState:
        """Return current AlarmState, creating a default entry if absent."""
        return self._get_or_create_state(appliance_id)

    def get_all_alarm_states(self) -> list[AlarmState]:
        return list(self.alarm_states.values())

    # ------------------------------------------------------------------
    # Cycle-event handlers (called by the polling scheduler)
    # ------------------------------------------------------------------

    async def on_appliance_done(self, appliance: Appliance) -> None:
        """Handle a cycle-completion event.

        If the alarm is armed → send critical FCM alert and reset the arm flag.
        Otherwise → send a standard push notification.
        """
        state = self._get_or_create_state(appliance.id)

        if state.is_armed_for_current_cycle:
            logger.info(
                "Alarm is ARMED for '%s' – sending critical alert.", appliance.id
            )
            await fcm_service.send_critical_alarm(appliance)
            # Reset the armed state so it does not fire again for the same cycle.
            state.is_armed_for_current_cycle = False
            state.triggered_at = datetime.utcnow()
        else:
            logger.info(
                "Alarm is NOT armed for '%s' – sending standard notification.", appliance.id
            )
            await fcm_service.send_standard_notification(appliance)

    async def on_appliance_status_change(
        self,
        appliance: Appliance,
        previous_status: ApplianceStatus,
    ) -> None:
        """React to a status transition detected by the polling scheduler.

        Transitions handled:
          RUNNING → DONE | IDLE  : cycle finished, fire appropriate notification.
          IDLE | DONE → RUNNING  : new cycle started, auto-reset the arm flag so
                                   the user can choose to arm fresh.
        """
        current_status = ApplianceStatus(appliance.status)
        previous = ApplianceStatus(previous_status)

        # --- Cycle completion ---
        if previous == ApplianceStatus.RUNNING and current_status in (
            ApplianceStatus.DONE,
            ApplianceStatus.IDLE,
        ):
            logger.info(
                "Appliance '%s' transitioned %s → %s (cycle done).",
                appliance.id,
                previous.value,
                current_status.value,
            )
            await self.on_appliance_done(appliance)

        # --- New cycle started: reset arm state ---
        elif previous in (ApplianceStatus.IDLE, ApplianceStatus.DONE) and current_status == ApplianceStatus.RUNNING:
            state = self._get_or_create_state(appliance.id)
            if state.is_armed_for_current_cycle:
                logger.info(
                    "New cycle started on '%s' – resetting arm state from previous cycle.",
                    appliance.id,
                )
            state.is_armed_for_current_cycle = False
            logger.info(
                "Appliance '%s' transitioned %s → %s (new cycle).",
                appliance.id,
                previous.value,
                current_status.value,
            )


# Singleton instance shared across the application.
alarm_manager = AlarmManager()
