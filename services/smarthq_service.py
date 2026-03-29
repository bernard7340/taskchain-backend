"""SmartHQ service using gehomesdk WebSocket client.

Each user gets their own GeWebsocketClient that maintains a persistent
WebSocket connection to GE's servers.  Appliance state is pushed in
real-time — no polling needed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from models.appliance import Appliance, ApplianceStatus, ApplianceType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

def _ge_type_to_our_type(ge_type_str: str) -> ApplianceType:
    s = ge_type_str.lower()
    if "dryer" in s:
        return ApplianceType.DRYER
    if "washer" in s or "laundry" in s:
        return ApplianceType.WASHER
    if "oven" in s or "range" in s or "cook" in s:
        return ApplianceType.OVEN
    return ApplianceType.WASHER


def _laundry_state_to_status(raw_state: Any) -> ApplianceStatus:
    if raw_state is None:
        return ApplianceStatus.UNKNOWN
    s = str(raw_state).upper()
    if any(k in s for k in ("RUNNING", "SENSING", "FILLING", "SOAKING",
                             "WASHING", "RINSING", "SPINNING", "DRYING",
                             "COOL_DOWN", "DELAY", "IN_USE")):
        return ApplianceStatus.RUNNING
    if any(k in s for k in ("END", "DONE", "COMPLETE", "FINISHED")):
        return ApplianceStatus.DONE
    return ApplianceStatus.IDLE


def _oven_state_to_status(raw_state: Any) -> ApplianceStatus:
    if raw_state is None:
        return ApplianceStatus.UNKNOWN
    s = str(raw_state).upper()
    if any(k in s for k in ("PREHEAT", "COOK", "BAKE", "BROIL",
                             "ROAST", "WARM", "PROOF", "CONV", "SELF_CLEAN")):
        return ApplianceStatus.RUNNING
    if any(k in s for k in ("DONE", "END", "COMPLETE")):
        return ApplianceStatus.DONE
    return ApplianceStatus.IDLE


def _extract_ge_appliance(ge_appliance: Any) -> Optional[Appliance]:
    """Convert a gehomesdk GeAppliance into our Appliance model."""
    try:
        from gehomesdk.erd import ErdCode

        mac = ge_appliance.mac_addr

        # Appliance type
        try:
            raw_type = ge_appliance.get_erd_value(ErdCode.APPLIANCE_TYPE)
            our_type = _ge_type_to_our_type(str(raw_type)) if raw_type else ApplianceType.WASHER
        except Exception:
            our_type = ApplianceType.WASHER

        # Display name
        name = {
            ApplianceType.WASHER: "Smart Washer",
            ApplianceType.DRYER: "Smart Dryer",
            ApplianceType.OVEN: "Smart Oven",
        }.get(our_type, "GE Appliance")

        # Status + details
        status = ApplianceStatus.IDLE
        status_detail = "Idle"
        minutes_remaining: Optional[int] = None

        if our_type in (ApplianceType.WASHER, ApplianceType.DRYER):
            try:
                machine_state = ge_appliance.get_erd_value(ErdCode.LAUNDRY_MACHINE_STATE)
                status = _laundry_state_to_status(machine_state)
            except Exception:
                pass
            try:
                tr = ge_appliance.get_erd_value(ErdCode.LAUNDRY_TIME_REMAINING)
                if tr is not None:
                    minutes_remaining = int(tr)
            except Exception:
                pass
            try:
                cycle = ge_appliance.get_erd_value(ErdCode.LAUNDRY_CYCLE)
                if cycle:
                    cycle_str = str(cycle).replace("_", " ").title()
                    if minutes_remaining:
                        status_detail = f"{cycle_str} · {minutes_remaining}m left"
                    else:
                        status_detail = cycle_str
                elif status == ApplianceStatus.RUNNING:
                    status_detail = "Running"
                elif status == ApplianceStatus.DONE:
                    status_detail = "Cycle complete"
            except Exception:
                if status == ApplianceStatus.RUNNING:
                    status_detail = "Running"
                elif status == ApplianceStatus.DONE:
                    status_detail = "Cycle complete"

        elif our_type == ApplianceType.OVEN:
            for code_name in ("UPPER_OVEN_CURRENT_STATE", "LOWER_OVEN_CURRENT_STATE"):
                try:
                    code = getattr(ErdCode, code_name)
                    oven_state = ge_appliance.get_erd_value(code)
                    if oven_state:
                        status = _oven_state_to_status(oven_state)
                        status_detail = str(oven_state).replace("_", " ").title()
                        break
                except Exception:
                    pass

        return Appliance(
            id=mac,
            name=name,
            type=our_type,
            status=status,
            status_detail=status_detail,
            minutes_remaining=minutes_remaining,
            is_active=(status == ApplianceStatus.RUNNING),
            last_updated=datetime.utcnow(),
        )
    except Exception as exc:
        logger.error("Failed to extract appliance %s: %s",
                     getattr(ge_appliance, "mac_addr", "?"), exc)
        return None


# ---------------------------------------------------------------------------
# SmartHQ Service
# ---------------------------------------------------------------------------

class SmartHQService:
    """Per-user SmartHQ WebSocket client wrapper."""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self._username: str = username or ""
        self._password: str = password or ""
        self._ge_client: Any = None
        self._run_task: Optional[asyncio.Task] = None
        self._ready_event: asyncio.Event = asyncio.Event()
        self._state_change_callback: Optional[Callable] = None

        if not self._username:
            try:
                from config import get_settings
                s = get_settings()
                self._username = s.smarthq_username
                self._password = s.smarthq_password
            except Exception:
                pass

    async def start(self) -> None:
        """Connect to GE SmartHQ and wait up to 20s for appliance discovery."""
        if not self._username or not self._password:
            logger.info("SmartHQ: no credentials, skipping.")
            return

        try:
            from gehomesdk import (GeWebsocketClient,
                                   EVENT_GOT_APPLIANCE_LIST,
                                   EVENT_APPLIANCE_STATE_CHANGE)
            from aiohttp import ClientSession

            self._ge_client = GeWebsocketClient(self._username, self._password)

            async def _on_got_list(data: Any) -> None:
                count = len(data) if data else 0
                logger.info("SmartHQ: appliance list received (%d devices).", count)
                self._ready_event.set()

            async def _on_state_change(data: Any) -> None:
                if self._state_change_callback:
                    ge_app = data[0] if isinstance(data, (list, tuple)) else data
                    our = _extract_ge_appliance(ge_app)
                    if our:
                        await self._state_change_callback(our)

            self._ge_client.add_event_handler(EVENT_GOT_APPLIANCE_LIST, _on_got_list)
            self._ge_client.add_event_handler(EVENT_APPLIANCE_STATE_CHANGE, _on_state_change)

            async def _run() -> None:
                async with ClientSession() as session:
                    await self._ge_client.async_get_credentials_and_run(session)

            self._run_task = asyncio.create_task(_run())

            # Wait up to 20s for the initial appliance list
            try:
                await asyncio.wait_for(asyncio.shield(self._ready_event.wait()), timeout=20.0)
                logger.info("SmartHQ: ready for %s (%d appliances).",
                            self._username, len(self._ge_client.appliances))
            except asyncio.TimeoutError:
                logger.warning("SmartHQ: timed out for %s — connection may still succeed.",
                               self._username)

        except Exception as exc:
            logger.error("SmartHQ start() failed for %s: %s", self._username, exc)

    async def start_with_auth_code(self, auth_code: str) -> None:
        """Connect to GE SmartHQ using an OAuth2 authorization code.

        The phone obtained this code by loading GE's login page in a WebView
        (not blocked by CAPTCHA).  We exchange it for tokens here on the
        server (this token endpoint call is not CAPTCHA-protected), then
        connect to the SmartHQ WebSocket directly without scraping HTML.
        """
        try:
            from gehomesdk import (GeWebsocketClient,
                                   EVENT_GOT_APPLIANCE_LIST,
                                   EVENT_APPLIANCE_STATE_CHANGE)
            from gehomesdk.clients.const import (
                OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET,
                OAUTH2_REDIRECT_URI, LOGIN_URL,
            )
            from aiohttp import ClientSession, BasicAuth

            # Exchange the auth code for access + refresh tokens.
            post_data = {
                "code": auth_code,
                "client_id": OAUTH2_CLIENT_ID,
                "client_secret": OAUTH2_CLIENT_SECRET,
                "redirect_uri": OAUTH2_REDIRECT_URI,
                "grant_type": "authorization_code",
            }
            async with ClientSession() as tmp:
                basic = BasicAuth(OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET)
                async with tmp.post(
                    f"{LOGIN_URL}/oauth2/token",
                    data=post_data,
                    auth=basic,
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        raise ValueError(
                            f"Token exchange failed ({resp.status}): {body}"
                        )
                    token_data = await resp.json()

            access_token: Optional[str] = token_data.get("access_token")
            refresh_token: Optional[str] = token_data.get("refresh_token")
            if not access_token:
                raise ValueError(f"No access_token in token response: {token_data}")

            logger.info("SmartHQ: token exchange succeeded — connecting WebSocket.")

            # Create a GeWebsocketClient, inject tokens, get WSS endpoint.
            self._ge_client = GeWebsocketClient("", "")

            async def _on_got_list(data: Any) -> None:
                count = len(data) if data else 0
                logger.info("SmartHQ: appliance list received (%d devices).", count)
                self._ready_event.set()

            async def _on_state_change(data: Any) -> None:
                if self._state_change_callback:
                    ge_app = data[0] if isinstance(data, (list, tuple)) else data
                    our = _extract_ge_appliance(ge_app)
                    if our:
                        await self._state_change_callback(our)

            self._ge_client.add_event_handler(EVENT_GOT_APPLIANCE_LIST, _on_got_list)
            self._ge_client.add_event_handler(EVENT_APPLIANCE_STATE_CHANGE, _on_state_change)

            async def _run() -> None:
                async with ClientSession() as session:
                    # Inject the tokens so the client skips HTML login.
                    self._ge_client._session = session
                    self._ge_client._access_token = access_token
                    self._ge_client._refresh_token = refresh_token
                    # Get WebSocket endpoint via the REST API (uses Bearer token).
                    wss_creds = await self._ge_client._async_get_wss_credentials()
                    self._ge_client.credentials = wss_creds
                    # Run WebSocket loop (reconnects use refresh token, not HTML login).
                    await self._ge_client.async_run_client()

            self._run_task = asyncio.create_task(_run())

            # Wait up to 20s for the initial appliance list.
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._ready_event.wait()), timeout=20.0
                )
                logger.info(
                    "SmartHQ: ready via OAuth code (%d appliances).",
                    len(self._ge_client.appliances),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "SmartHQ: timed out waiting for appliances — "
                    "connection may still succeed in the background."
                )

        except Exception as exc:
            logger.error("SmartHQ start_with_auth_code() failed: %s", exc)
            raise

    async def stop(self) -> None:
        if self._ge_client:
            try:
                self._ge_client.disconnect()
            except Exception:
                pass
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass
            self._run_task = None
        self._ge_client = None

    async def fetch_all_appliances(self) -> List[Appliance]:
        """Return current appliance state from the live WebSocket connection."""
        if not self._ge_client:
            return []
        results = []
        for ge_app in self._ge_client.appliances.values():
            our = _extract_ge_appliance(ge_app)
            if our:
                results.append(our)
        return results

    def set_state_change_callback(self, callback: Callable) -> None:
        self._state_change_callback = callback

    @property
    def is_ready(self) -> bool:
        return self._ge_client is not None and self._ready_event.is_set()


# Legacy singleton used by health-check only.
smarthq_service = SmartHQService()
