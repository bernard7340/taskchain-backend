"""SmartHQ Pro API service.

Fetches GE appliance state from the SmartHQ REST API and maps the raw
payload to the internal Appliance model.

Multi-user design
-----------------
``SmartHQService`` is now **instantiated per user** by the
``UserSessionManager``.  Pass ``username`` and ``password`` directly to the
constructor instead of reading them from global settings.

The class still supports the legacy singleton pattern (used by the global
``smarthq_service`` instance at the bottom of this module) so that the health
check endpoint continues to work without changes.

gehomesdk integration note
---------------------------
The ``gehomesdk`` library (``pip install gehomesdk``) provides a higher-level
async client for GE Appliances / SmartHQ via OAuth2.  If it is installed and
you want to use it instead of the raw REST approach, replace the ``_login``
and ``fetch_all_appliances`` implementations below with:

    # TODO: gehomesdk integration
    # from gehomesdk import GeApiClient
    #
    # client = GeApiClient(username, password)
    # await client.async_login()
    # raw_appliances = await client.async_get_appliances()
    # for raw in raw_appliances:
    #     appliance_id = str(raw.appliance_id)
    #     name = raw.name or appliance_id
    #     # Map raw.status to ApplianceStatus using _SMARTHQ_STATUS_MAP
    #     ...

The current implementation uses the SmartHQ REST API directly with httpx,
which works without the SDK but requires a valid bearer token or
username/password credentials.

SmartHQ state → ApplianceStatus mapping
-----------------------------------------
SmartHQ "operationMode" or "applianceState" values seen in practice:

  IDLE / STANDBY           → IDLE
  RUNNING / IN_USE         → RUNNING
  END_OF_CYCLE / COMPLETE  → DONE
  FAULT / ERROR            → ERROR
  anything else            → UNKNOWN
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from models.appliance import Appliance, ApplianceStatus, ApplianceType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SmartHQ state vocabulary
# ---------------------------------------------------------------------------

_SMARTHQ_STATUS_MAP: Dict[str, ApplianceStatus] = {
    # Idle / off
    "IDLE": ApplianceStatus.IDLE,
    "STANDBY": ApplianceStatus.IDLE,
    "OFF": ApplianceStatus.IDLE,
    "POWERED_OFF": ApplianceStatus.IDLE,
    # Running
    "RUNNING": ApplianceStatus.RUNNING,
    "IN_USE": ApplianceStatus.RUNNING,
    "ACTIVE": ApplianceStatus.RUNNING,
    "SENSING": ApplianceStatus.RUNNING,
    "DELAY_START": ApplianceStatus.RUNNING,
    "PAUSED": ApplianceStatus.RUNNING,
    # Done
    "END_OF_CYCLE": ApplianceStatus.DONE,
    "COMPLETE": ApplianceStatus.DONE,
    "DONE": ApplianceStatus.DONE,
    "FINISHED": ApplianceStatus.DONE,
    # Errors
    "FAULT": ApplianceStatus.ERROR,
    "ERROR": ApplianceStatus.ERROR,
}

_SMARTHQ_TYPE_MAP: Dict[str, ApplianceType] = {
    "washer": ApplianceType.WASHER,
    "dryer": ApplianceType.DRYER,
    "oven": ApplianceType.OVEN,
    "range": ApplianceType.OVEN,
    "dishwasher": ApplianceType.WASHER,
}

# Base URL for SmartHQ REST API
_SMARTHQ_BASE_URL = "https://api.smarthq.com/v1"


def _map_status(raw_status: str) -> ApplianceStatus:
    return _SMARTHQ_STATUS_MAP.get(raw_status.upper(), ApplianceStatus.UNKNOWN)


def _map_type(raw_type: str) -> ApplianceType:
    return _SMARTHQ_TYPE_MAP.get(raw_type.lower(), ApplianceType.WASHER)


def _build_status_detail(appliance_data: Dict[str, Any]) -> str:
    """Compose a human-readable status detail string from raw appliance data."""
    attributes = appliance_data.get("attributes", {})
    cycle_name: str = (
        attributes.get("cycleName")
        or attributes.get("selectedCycle")
        or attributes.get("cycleSelected")
        or ""
    )
    time_remaining: Optional[int] = (
        attributes.get("timeRemaining")
        or attributes.get("remainingTime")
        or attributes.get("cycleTimeRemaining")
    )

    raw_status: str = (
        appliance_data.get("applianceState")
        or appliance_data.get("operationMode")
        or "UNKNOWN"
    )
    status = _map_status(raw_status)

    if status == ApplianceStatus.RUNNING:
        parts = []
        if cycle_name:
            parts.append(cycle_name.replace("_", " ").title())
        if time_remaining is not None:
            parts.append(f"{time_remaining} mins left")
        return " · ".join(parts) if parts else "Running"
    elif status == ApplianceStatus.DONE:
        return "Cycle complete"
    elif status == ApplianceStatus.IDLE:
        return "Idle"
    elif status == ApplianceStatus.ERROR:
        fault = attributes.get("faultCode") or attributes.get("errorCode") or "Unknown error"
        return f"Error: {fault}"
    return "Unknown"


def _parse_appliance(raw: Dict[str, Any]) -> Appliance:
    """Convert a raw SmartHQ appliance dict to an Appliance model."""
    appliance_id: str = str(raw.get("applianceId") or raw.get("id") or "unknown")
    name: str = raw.get("nickname") or raw.get("name") or raw.get("modelNumber") or appliance_id
    raw_type: str = raw.get("type") or raw.get("applianceType") or "washer"
    raw_status: str = (
        raw.get("applianceState") or raw.get("operationMode") or "UNKNOWN"
    )

    attributes = raw.get("attributes", {})
    time_remaining: Optional[int] = (
        attributes.get("timeRemaining")
        or attributes.get("remainingTime")
        or attributes.get("cycleTimeRemaining")
    )

    status = _map_status(raw_status)

    return Appliance(
        id=appliance_id,
        name=name,
        type=_map_type(raw_type),
        status=status,
        status_detail=_build_status_detail(raw),
        minutes_remaining=int(time_remaining) if time_remaining is not None else None,
        is_active=(status == ApplianceStatus.RUNNING),
        last_updated=datetime.utcnow(),
    )


class SmartHQService:
    """Async client for the SmartHQ Pro REST API.

    Can be used as a singleton (legacy) or as a per-user instance.
    Pass ``username`` and ``password`` to use per-user credentials; otherwise
    the class falls back to reading from the global ``Settings``.
    """

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: str = _SMARTHQ_BASE_URL,
    ) -> None:
        # Explicit per-user credentials take priority over settings.
        self._username: str = username or ""
        self._password: str = password or ""
        self._bearer_token: str = api_key or ""
        self._base_url: str = base_url
        self._client: Optional[httpx.AsyncClient] = None

        # If no explicit credentials and no api_key, try reading from settings
        # (legacy singleton path only).
        if not self._username and not self._bearer_token:
            try:
                from config import get_settings
                settings = get_settings()
                self._bearer_token = settings.smarthq_api_key
                self._username = settings.smarthq_username
                self._password = settings.smarthq_password
                self._base_url = settings.smarthq_base_url
            except Exception:
                pass  # Settings may not be available in tests

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the shared httpx client and authenticate if needed."""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(15.0),
        )
        if not self._bearer_token and self._username:
            await self._login()

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _login(self) -> None:
        """Obtain a bearer token via username/password login."""
        assert self._client is not None
        try:
            response = await self._client.post(
                "/auth/token",
                json={
                    "username": self._username,
                    "password": self._password,
                    "grant_type": "password",
                },
            )
            response.raise_for_status()
            data = response.json()
            self._bearer_token = data.get("access_token") or data.get("token") or ""
            logger.info("SmartHQ login successful for user %s.", self._username)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "SmartHQ login failed for %s (%s): %s",
                self._username,
                exc.response.status_code,
                exc,
            )
            raise
        except Exception as exc:
            logger.error("SmartHQ login error for %s: %s", self._username, exc)
            raise

    def _auth_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        return headers

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    async def _get(self, path: str) -> Optional[Any]:
        """Perform a GET request, refreshing the token once on 401."""
        assert self._client is not None, "SmartHQService.start() was not called"

        for attempt in range(2):
            try:
                response = await self._client.get(path, headers=self._auth_headers())
                if response.status_code == 401 and attempt == 0:
                    logger.warning("SmartHQ 401 – attempting re-login for %s.", self._username)
                    await self._login()
                    continue
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "SmartHQ HTTP error on GET %s (%s): %s",
                    path,
                    exc.response.status_code,
                    exc,
                )
                return None
            except Exception as exc:
                logger.error("SmartHQ request error on GET %s: %s", path, exc)
                return None
        return None

    async def fetch_all_appliances(self) -> List[Appliance]:
        """Fetch and parse all appliances from SmartHQ.

        TODO: If using gehomesdk, replace this method body with:

            from gehomesdk import GeApiClient
            # GeApiClient is already authenticated (constructed with username/password
            # and logged in via async_login()).  Call:
            #   raw_appliances = await ge_client.async_get_appliances()
            # Then map each item to our internal Appliance model using _parse_appliance()
            # or a custom mapping.
        """
        if self._client is None:
            logger.warning(
                "SmartHQService.fetch_all_appliances called before start() — returning empty list."
            )
            return []

        data = await self._get("/appliances")
        if data is None:
            return []

        # The API may return a list directly or wrap it in a key.
        raw_list: List[Dict[str, Any]]
        if isinstance(data, list):
            raw_list = data
        elif isinstance(data, dict):
            raw_list = (
                data.get("appliances")
                or data.get("data")
                or data.get("items")
                or []
            )
        else:
            logger.warning("Unexpected SmartHQ response shape: %s", type(data))
            return []

        appliances: List[Appliance] = []
        for raw in raw_list:
            try:
                appliances.append(_parse_appliance(raw))
            except Exception as exc:
                logger.error("Failed to parse appliance: %s – %s", raw, exc)

        logger.debug("SmartHQ: fetched %d appliances.", len(appliances))
        return appliances

    async def fetch_appliance(self, appliance_id: str) -> Optional[Appliance]:
        """Fetch a single appliance by ID."""
        if self._client is None:
            return None
        data = await self._get(f"/appliances/{appliance_id}")
        if data is None:
            return None
        try:
            if isinstance(data, dict) and "appliance" in data:
                data = data["appliance"]
            return _parse_appliance(data)
        except Exception as exc:
            logger.error("Failed to parse appliance %s: %s", appliance_id, exc)
            return None

    @property
    def is_ready(self) -> bool:
        """True if the client is initialised and has a token."""
        return self._client is not None and bool(self._bearer_token)


# ---------------------------------------------------------------------------
# Legacy singleton — kept for health-check compatibility.
# The per-user sessions use their own SmartHQService instances instead.
# ---------------------------------------------------------------------------

smarthq_service = SmartHQService()
