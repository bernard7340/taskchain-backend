"""Roborock MQTT service.

Uses the ``python-roborock`` library to connect to the Roborock cloud and
receive real-time vacuum state updates.  The service maintains a local
Appliance model for each discovered Roborock device and exposes it through
the same interface as the SmartHQ service so the scheduler can treat all
appliances uniformly.

Roborock status → ApplianceStatus mapping
------------------------------------------
Roborock ``state`` integer values (RoborockStateCode):

  1  = Starting          → RUNNING
  2  = Charger Disconnected → IDLE
  3  = Idle              → IDLE
  4  = Remote Control    → RUNNING
  5  = Cleaning          → RUNNING
  6  = Returning To Dock → RUNNING   (cycle winding down but not "done" yet)
  7  = Manual Mode       → RUNNING
  8  = Charging          → IDLE
  9  = Charging Error    → ERROR
  10 = Paused            → RUNNING
  11 = Spot Cleaning     → RUNNING
  12 = Error             → ERROR
  13 = Shutting Down     → IDLE
  14 = Updating          → IDLE
  15 = Docking           → RUNNING
  16 = Go To             → RUNNING
  17 = Zone Cleaning     → RUNNING
  18 = Room Cleaning     → RUNNING
  22 = Emptying Dustbin  → RUNNING
  26 = Charging Complete → DONE
  28 = Segment Cleaning  → RUNNING
  29 = Emptying Dustbin Cleaning → RUNNING
  100 = Full             → ERROR
  101 = Offline          → UNKNOWN
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import get_settings
from models.appliance import Appliance, ApplianceStatus, ApplianceType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State code mapping (integer → ApplianceStatus)
# ---------------------------------------------------------------------------

_STATE_MAP: Dict[int, ApplianceStatus] = {
    1: ApplianceStatus.RUNNING,   # Starting
    2: ApplianceStatus.IDLE,      # Charger Disconnected
    3: ApplianceStatus.IDLE,      # Idle
    4: ApplianceStatus.RUNNING,   # Remote Control
    5: ApplianceStatus.RUNNING,   # Cleaning
    6: ApplianceStatus.RUNNING,   # Returning To Dock
    7: ApplianceStatus.RUNNING,   # Manual Mode
    8: ApplianceStatus.IDLE,      # Charging
    9: ApplianceStatus.ERROR,     # Charging Error
    10: ApplianceStatus.RUNNING,  # Paused
    11: ApplianceStatus.RUNNING,  # Spot Cleaning
    12: ApplianceStatus.ERROR,    # Error
    13: ApplianceStatus.IDLE,     # Shutting Down
    14: ApplianceStatus.IDLE,     # Updating
    15: ApplianceStatus.RUNNING,  # Docking
    16: ApplianceStatus.RUNNING,  # Go To
    17: ApplianceStatus.RUNNING,  # Zone Cleaning
    18: ApplianceStatus.RUNNING,  # Room Cleaning
    22: ApplianceStatus.RUNNING,  # Emptying Dustbin
    26: ApplianceStatus.DONE,     # Charging Complete (cycle done)
    28: ApplianceStatus.RUNNING,  # Segment Cleaning
    29: ApplianceStatus.RUNNING,  # Emptying Dustbin Cleaning
    100: ApplianceStatus.ERROR,   # Full
    101: ApplianceStatus.UNKNOWN, # Offline
}

_STATE_LABELS: Dict[int, str] = {
    1: "Starting",
    2: "Idle",
    3: "Idle",
    4: "Remote Control",
    5: "Cleaning",
    6: "Returning to Dock",
    7: "Manual Mode",
    8: "Charging",
    9: "Charging Error",
    10: "Paused",
    11: "Spot Cleaning",
    12: "Error",
    13: "Shutting Down",
    14: "Updating",
    15: "Docking",
    16: "Going to Location",
    17: "Zone Cleaning",
    18: "Room Cleaning",
    22: "Emptying Dustbin",
    26: "Charging Complete",
    28: "Segment Cleaning",
    29: "Emptying & Cleaning",
    100: "Dustbin Full",
    101: "Offline",
}


def _state_code_to_status(state_code: int) -> ApplianceStatus:
    return _STATE_MAP.get(state_code, ApplianceStatus.UNKNOWN)


def _state_code_to_label(state_code: int) -> str:
    return _STATE_LABELS.get(state_code, f"State {state_code}")


class RoborockService:
    """Manages Roborock device connections via python-roborock."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._appliances: Dict[str, Appliance] = {}
        self._client: Optional[Any] = None  # python-roborock client
        self._connected: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to the Roborock MQTT broker and discover devices."""
        if not self._settings.roborock_username or not self._settings.roborock_password:
            logger.warning(
                "Roborock credentials not configured – Roborock service disabled."
            )
            return

        try:
            await self._connect()
        except Exception as exc:
            logger.error("Failed to start Roborock service: %s", exc)

    async def stop(self) -> None:
        """Disconnect from Roborock MQTT."""
        if self._client is not None:
            try:
                await self._disconnect()
            except Exception as exc:
                logger.error("Error during Roborock disconnect: %s", exc)
        self._connected = False

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        """Establish the Roborock MQTT connection using python-roborock."""
        try:
            # python-roborock ≥ 0.12 API
            from roborock import RoborockApiClient  # type: ignore[import]
            from roborock.containers import UserData  # type: ignore[import]

            api_client = RoborockApiClient(self._settings.roborock_username)
            user_data: UserData = await api_client.pass_login(
                self._settings.roborock_password
            )

            from roborock.local_api import RoborockLocalClient  # type: ignore[import]
            from roborock.cloud_api import RoborockMqttClient  # type: ignore[import]

            home_data = await api_client.get_home_data(user_data)
            self._client = RoborockMqttClient(user_data, home_data)
            await self._client.connect()
            self._connected = True

            # Seed the appliance cache from discovered devices.
            await self._refresh_device_list(home_data)
            logger.info("Roborock MQTT connected. Devices: %s", list(self._appliances.keys()))

        except ImportError:
            logger.error(
                "python-roborock is not installed or the import path changed. "
                "Install it with: pip install python-roborock"
            )
        except Exception as exc:
            logger.error("Roborock connection error: %s", exc)
            self._connected = False

    async def _disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    async def _refresh_device_list(self, home_data: Any) -> None:
        """Populate _appliances from the Roborock home data."""
        try:
            devices = home_data.devices if hasattr(home_data, "devices") else []
            for device in devices:
                device_id = str(getattr(device, "duid", None) or getattr(device, "id", "unknown"))
                name = getattr(device, "name", None) or f"Roborock {device_id}"
                self._appliances[device_id] = Appliance(
                    id=device_id,
                    name=name,
                    type=ApplianceType.ROBOROCK,
                    status=ApplianceStatus.UNKNOWN,
                    status_detail="Connecting…",
                    last_updated=datetime.utcnow(),
                )
        except Exception as exc:
            logger.error("Failed to refresh Roborock device list: %s", exc)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def fetch_all_appliances(self) -> List[Appliance]:
        """Poll each known Roborock device for its current status."""
        if not self._connected or self._client is None:
            logger.debug("Roborock not connected – returning cached data.")
            return list(self._appliances.values())

        updated: List[Appliance] = []
        for device_id in list(self._appliances.keys()):
            appliance = await self._fetch_device_status(device_id)
            if appliance:
                self._appliances[device_id] = appliance
                updated.append(appliance)
            else:
                updated.append(self._appliances[device_id])

        return updated

    async def _fetch_device_status(self, device_id: str) -> Optional[Appliance]:
        """Query a single Roborock device for its status."""
        assert self._client is not None

        try:
            # python-roborock exposes get_status() which returns a DeviceStatus.
            status_data = await self._client.get_status(device_id)
            return self._parse_device_status(device_id, status_data)
        except asyncio.TimeoutError:
            logger.warning("Timeout fetching status for Roborock device %s.", device_id)
            return None
        except Exception as exc:
            logger.error("Error fetching status for Roborock device %s: %s", device_id, exc)
            return None

    def _parse_device_status(self, device_id: str, status_data: Any) -> Appliance:
        """Convert raw python-roborock status data to Appliance model."""
        cached = self._appliances.get(device_id)
        name = cached.name if cached else f"Roborock {device_id}"

        # python-roborock DeviceStatus has a .state attribute (int or enum).
        state_value: int = 3  # default: Idle
        battery: Optional[int] = None
        fan_power: Optional[int] = None

        try:
            raw_state = getattr(status_data, "state", None)
            if raw_state is not None:
                state_value = int(raw_state)
            battery = getattr(status_data, "battery", None)
            fan_power = getattr(status_data, "fan_power", None)
        except (TypeError, ValueError):
            pass

        appliance_status = _state_code_to_status(state_value)
        label = _state_code_to_label(state_value)

        detail_parts = [label]
        if battery is not None:
            detail_parts.append(f"Battery {battery}%")
        if fan_power is not None and appliance_status == ApplianceStatus.RUNNING:
            detail_parts.append(f"Fan {fan_power}%")

        return Appliance(
            id=device_id,
            name=name,
            type=ApplianceType.ROBOROCK,
            status=appliance_status,
            status_detail=" · ".join(detail_parts),
            minutes_remaining=None,
            is_active=(appliance_status == ApplianceStatus.RUNNING),
            last_updated=datetime.utcnow(),
        )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def get_appliance(self, device_id: str) -> Optional[Appliance]:
        return self._appliances.get(device_id)

    @property
    def is_connected(self) -> bool:
        return self._connected


# Singleton instance.
roborock_service = RoborockService()
