"""Multi-user session manager for TaskChain cloud deployment.

Each authenticated user gets an isolated ``UserSession`` that holds:
  - Their encrypted SmartHQ credentials.
  - A dedicated ``SmartHQService`` instance (no cross-user credential sharing).
  - Per-user appliance cache and alarm states.
  - Their current FCM device token.

The ``UserSessionManager`` singleton is the single source of truth for all
active sessions.  The polling scheduler calls ``poll_all_users()`` on a fixed
interval; each user's SmartHQ client is used independently.

Security notes
--------------
- SmartHQ passwords are encrypted with Fernet before storage (see utils/crypto.py).
- The encryption key must be supplied via ``ENCRYPTION_KEY`` env var.
- Users are identified solely by their Firebase UID — the app layer guarantees
  that the UID is authoritative (JWT verified by firebase_auth middleware).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from models.alarm import AlarmState
from models.appliance import Appliance, ApplianceStatus
from services.smarthq_service import SmartHQService
from services import fcm_service
from utils.crypto import decrypt_safe, encrypt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class UserSession:
    """All runtime state for a single authenticated user."""

    user_id: str
    smarthq_username: str
    # Password stored encrypted; call _plaintext_password() to read it.
    _smarthq_password_enc: bytes = field(repr=False)
    fcm_token: str

    # Per-user SmartHQ client – created in register_user(), closed in unregister_user().
    smarthq_client: SmartHQService = field(default_factory=SmartHQService)

    # Appliance cache keyed by appliance_id.
    appliances: Dict[str, Appliance] = field(default_factory=dict)

    # Alarm states keyed by appliance_id.
    alarm_states: Dict[str, AlarmState] = field(default_factory=dict)

    last_poll: Optional[datetime] = None

    def plaintext_password(self) -> Optional[str]:
        """Decrypt and return the SmartHQ password, or None on failure."""
        return decrypt_safe(self._smarthq_password_enc)

    # ------------------------------------------------------------------
    # Alarm helpers
    # ------------------------------------------------------------------

    def get_or_create_alarm_state(self, appliance_id: str) -> AlarmState:
        if appliance_id not in self.alarm_states:
            self.alarm_states[appliance_id] = AlarmState(appliance_id=appliance_id)
        return self.alarm_states[appliance_id]

    async def arm_alarm(self, appliance_id: str) -> AlarmState:
        state = self.get_or_create_alarm_state(appliance_id)
        state.is_armed_for_current_cycle = True
        state.armed_at = datetime.utcnow()
        logger.info("Alarm ARMED for user=%s appliance=%s", self.user_id, appliance_id)
        return state

    async def disarm_alarm(self, appliance_id: str) -> AlarmState:
        state = self.get_or_create_alarm_state(appliance_id)
        state.is_armed_for_current_cycle = False
        logger.info("Alarm DISARMED for user=%s appliance=%s", self.user_id, appliance_id)
        return state

    async def on_appliance_done(self, appliance: Appliance) -> None:
        """Send the correct FCM notification when a cycle completes."""
        state = self.get_or_create_alarm_state(appliance.id)
        if state.is_armed_for_current_cycle:
            logger.info(
                "Alarm ARMED for user=%s appliance=%s – sending critical alert.",
                self.user_id,
                appliance.id,
            )
            await fcm_service.send_critical_alarm_to(appliance, self.fcm_token)
            state.is_armed_for_current_cycle = False
            state.triggered_at = datetime.utcnow()
        else:
            logger.info(
                "Alarm NOT armed for user=%s appliance=%s – sending standard notification.",
                self.user_id,
                appliance.id,
            )
            await fcm_service.send_standard_notification_to(appliance, self.fcm_token)

    async def on_appliance_status_change(
        self,
        appliance: Appliance,
        previous_status: ApplianceStatus,
    ) -> None:
        """React to a status transition detected during polling."""
        current = ApplianceStatus(appliance.status)
        previous = ApplianceStatus(previous_status)

        # Cycle completion: RUNNING → DONE | IDLE
        if previous == ApplianceStatus.RUNNING and current in (
            ApplianceStatus.DONE,
            ApplianceStatus.IDLE,
        ):
            logger.info(
                "user=%s appliance=%s: %s → %s (cycle done).",
                self.user_id,
                appliance.id,
                previous.value,
                current.value,
            )
            await self.on_appliance_done(appliance)

        # New cycle started: IDLE | DONE → RUNNING — reset arm state.
        elif previous in (ApplianceStatus.IDLE, ApplianceStatus.DONE) and current == ApplianceStatus.RUNNING:
            state = self.get_or_create_alarm_state(appliance.id)
            state.is_armed_for_current_cycle = False
            logger.info(
                "user=%s appliance=%s: %s → %s (new cycle started, arm state reset).",
                self.user_id,
                appliance.id,
                previous.value,
                current.value,
            )


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------


class UserSessionManager:
    """Thread-safe registry of all active user sessions.

    All mutations are protected by an asyncio lock to prevent data races
    when multiple HTTP handlers and the polling scheduler run concurrently.
    """

    def __init__(self) -> None:
        # Keyed by Firebase UID.
        self._sessions: Dict[str, UserSession] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register_user(
        self,
        user_id: str,
        smarthq_username: str,
        smarthq_password: str,
        fcm_token: str,
    ) -> UserSession:
        """Create (or replace) a session for *user_id*.

        1. Encrypts the SmartHQ password.
        2. Creates a dedicated SmartHQService and authenticates it.
        3. Performs an initial appliance fetch so the caller gets live data
           immediately.
        4. Stores the session.
        """
        encrypted_pw = encrypt(smarthq_password)

        # Build a per-user SmartHQService with the provided credentials.
        client = SmartHQService(
            username=smarthq_username,
            password=smarthq_password,
        )
        await client.start()

        session = UserSession(
            user_id=user_id,
            smarthq_username=smarthq_username,
            _smarthq_password_enc=encrypted_pw,
            fcm_token=fcm_token,
            smarthq_client=client,
        )

        # Initial fetch — populate the appliance cache right away.
        try:
            appliances = await client.fetch_all_appliances()
            for a in appliances:
                session.appliances[a.id] = a
                session.get_or_create_alarm_state(a.id)
            session.last_poll = datetime.utcnow()
            logger.info(
                "user=%s registered. Discovered %d appliances.",
                user_id,
                len(appliances),
            )
        except Exception as exc:
            logger.error(
                "user=%s initial appliance fetch failed: %s", user_id, exc
            )

        async with self._lock:
            # If there's an existing session, shut down its client first.
            existing = self._sessions.get(user_id)
            if existing is not None:
                await self._close_session_client(existing)
            self._sessions[user_id] = session

        return session

    async def register_user_with_code(
        self,
        user_id: str,
        auth_code: str,
        fcm_token: str,
    ) -> UserSession:
        """Create (or replace) a session using a GE OAuth2 authorization code.

        The phone completed the GE login in a WebView and sends us the
        authorization code.  We exchange it for tokens here and connect to
        SmartHQ without ever storing a username/password.
        """
        client = SmartHQService()
        await client.start_with_auth_code(auth_code)

        # Placeholder encrypted bytes (no password in this flow).
        encrypted_pw = encrypt("oauth2")

        session = UserSession(
            user_id=user_id,
            smarthq_username="oauth2",
            _smarthq_password_enc=encrypted_pw,
            fcm_token=fcm_token,
            smarthq_client=client,
        )

        try:
            appliances = await client.fetch_all_appliances()
            for a in appliances:
                session.appliances[a.id] = a
                session.get_or_create_alarm_state(a.id)
            session.last_poll = datetime.utcnow()
            logger.info(
                "user=%s registered via OAuth code. Discovered %d appliances.",
                user_id,
                len(appliances),
            )
        except Exception as exc:
            logger.error("user=%s initial appliance fetch failed: %s", user_id, exc)

        async with self._lock:
            existing = self._sessions.get(user_id)
            if existing is not None:
                await self._close_session_client(existing)
            self._sessions[user_id] = session

        return session

    async def unregister_user(self, user_id: str) -> bool:
        """Remove the session for *user_id* and close its SmartHQ client.

        Returns True if a session existed, False otherwise.
        """
        async with self._lock:
            session = self._sessions.pop(user_id, None)

        if session is None:
            logger.warning("Tried to unregister unknown user=%s", user_id)
            return False

        await self._close_session_client(session)
        logger.info("user=%s unregistered.", user_id)
        return True

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    async def get_session(self, user_id: str) -> Optional[UserSession]:
        """Return the session for *user_id*, or None if not registered."""
        async with self._lock:
            return self._sessions.get(user_id)

    async def update_fcm_token(self, user_id: str, fcm_token: str) -> bool:
        """Update the FCM token for an existing session.

        Returns True on success, False if the session does not exist.
        """
        async with self._lock:
            session = self._sessions.get(user_id)
            if session is None:
                return False
            session.fcm_token = fcm_token
            logger.info("FCM token updated for user=%s", user_id)
            return True

    def get_all_sessions(self) -> List[UserSession]:
        """Return a snapshot of all active sessions (no lock needed for reads)."""
        return list(self._sessions.values())

    def active_user_count(self) -> int:
        return len(self._sessions)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def poll_all_users(self) -> None:
        """Poll SmartHQ for every registered user and process state changes.

        Called by the APScheduler job every ``POLL_INTERVAL_SMARTHQ`` seconds.
        Each user's poll is run concurrently via asyncio.gather for efficiency.
        """
        sessions = self.get_all_sessions()
        if not sessions:
            logger.debug("poll_all_users: no active sessions.")
            return

        logger.debug("poll_all_users: polling %d user(s).", len(sessions))
        tasks = [self._poll_user(session) for session in sessions]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for session, result in zip(sessions, results):
            if isinstance(result, Exception):
                logger.error("poll_all_users: error for user=%s: %s", session.user_id, result)

    async def _poll_user(self, session: UserSession) -> None:
        """Fetch and diff SmartHQ state for a single user."""
        try:
            appliances = await session.smarthq_client.fetch_all_appliances()
        except Exception as exc:
            logger.error("SmartHQ fetch failed for user=%s: %s", session.user_id, exc)
            return

        for new_appliance in appliances:
            previous = session.appliances.get(new_appliance.id)
            session.appliances[new_appliance.id] = new_appliance
            session.get_or_create_alarm_state(new_appliance.id)

            if previous is None:
                logger.info(
                    "user=%s discovered appliance=%s status=%s",
                    session.user_id,
                    new_appliance.id,
                    new_appliance.status,
                )
                continue

            prev_status = ApplianceStatus(previous.status)
            new_status = ApplianceStatus(new_appliance.status)
            if prev_status != new_status:
                logger.info(
                    "user=%s appliance=%s: %s → %s",
                    session.user_id,
                    new_appliance.name,
                    prev_status.value,
                    new_status.value,
                )
                await session.on_appliance_status_change(new_appliance, prev_status)

        session.last_poll = datetime.utcnow()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _close_session_client(session: UserSession) -> None:
        try:
            await session.smarthq_client.stop()
        except Exception as exc:
            logger.warning(
                "Error closing SmartHQ client for user=%s: %s", session.user_id, exc
            )


# Singleton — imported by routers and the scheduler.
user_session_manager = UserSessionManager()
