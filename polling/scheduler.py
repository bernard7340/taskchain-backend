"""APScheduler-based background polling for TaskChain.

Multi-user cloud design
-----------------------
The primary job is ``poll_all_users``, which delegates to
``UserSessionManager.poll_all_users()``.  That method iterates every active
user session, calls their private SmartHQ client, and fires the appropriate
FCM notifications per-user.

The legacy ``poll_smarthq`` and ``poll_roborock`` jobs (single-user era) are
retained so that the old ``smarthq_service`` singleton and ``roborock_service``
singleton still function when credentials are configured in ``.env``.  They
write to the shared ``appliance_cache`` and use the global ``alarm_manager``
exactly as before.  This means the existing `/api/v1/appliances` and
`/api/v1/alarms` endpoints still work for operators running a single-user
self-hosted instance.

New deployments should use the multi-user ``/api/v1/users/*`` endpoints
instead.

Polling intervals
-----------------
  - Multi-user SmartHQ poll : ``POLL_INTERVAL_SMARTHQ`` (default 30 s)
  - Legacy SmartHQ poll     : same interval as above
  - Legacy Roborock poll    : ``POLL_INTERVAL_ROBOROCK`` (default 15 s)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import get_settings
from models.appliance import Appliance, ApplianceStatus
from services.alarm_manager import alarm_manager
from services.smarthq_service import smarthq_service
from services.roborock_service import roborock_service
from services.user_session_manager import user_session_manager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legacy shared in-memory appliance cache (single-user / self-hosted path)
# Keyed by appliance_id.  Updated by both legacy polling jobs.
# ---------------------------------------------------------------------------

appliance_cache: Dict[str, Appliance] = {}


def get_all_cached_appliances() -> list[Appliance]:
    return list(appliance_cache.values())


def get_cached_appliance(appliance_id: str) -> Appliance | None:
    return appliance_cache.get(appliance_id)


def upsert_appliance(appliance: Appliance) -> None:
    """Insert or replace an appliance in the legacy cache."""
    appliance_cache[appliance.id] = appliance


# ---------------------------------------------------------------------------
# Legacy appliance processing (single-user alarm_manager path)
# ---------------------------------------------------------------------------


async def _process_appliance_update(new_appliance: Appliance) -> None:
    """Diff the new appliance state against the legacy cache and fire alarm events."""
    previous = appliance_cache.get(new_appliance.id)
    appliance_cache[new_appliance.id] = new_appliance

    if previous is None:
        logger.info(
            "Discovered appliance '%s' (%s) with status %s.",
            new_appliance.name,
            new_appliance.id,
            new_appliance.status,
        )
        alarm_manager.get_alarm_state(new_appliance.id)
        return

    previous_status = ApplianceStatus(previous.status)
    new_status = ApplianceStatus(new_appliance.status)

    if previous_status != new_status:
        logger.info(
            "Status change for '%s': %s → %s",
            new_appliance.name,
            previous_status.value,
            new_status.value,
        )
        await alarm_manager.on_appliance_status_change(new_appliance, previous_status)


# ---------------------------------------------------------------------------
# Polling jobs
# ---------------------------------------------------------------------------


async def poll_all_users() -> None:
    """Multi-user poll — iterate all active user sessions.

    Called every ``POLL_INTERVAL_SMARTHQ`` seconds.
    """
    logger.debug("poll_all_users: starting [%s]", datetime.utcnow().isoformat())
    try:
        await user_session_manager.poll_all_users()
    except Exception as exc:
        logger.error("Unhandled error in poll_all_users: %s", exc)


async def poll_smarthq() -> None:
    """Legacy single-user SmartHQ poll.

    Only active when ``SMARTHQ_USERNAME`` / ``SMARTHQ_API_KEY`` is set in
    the environment.  Writes to the shared ``appliance_cache``.
    """
    logger.debug("Polling SmartHQ (legacy)… [%s]", datetime.utcnow().isoformat())
    try:
        appliances = await smarthq_service.fetch_all_appliances()
        for appliance in appliances:
            await _process_appliance_update(appliance)
    except Exception as exc:
        logger.error("Unhandled error in poll_smarthq (legacy): %s", exc)


async def poll_roborock() -> None:
    """Legacy Roborock poll.  Writes to the shared ``appliance_cache``."""
    logger.debug("Polling Roborock… [%s]", datetime.utcnow().isoformat())
    try:
        appliances = await roborock_service.fetch_all_appliances()
        for appliance in appliances:
            await _process_appliance_update(appliance)
    except Exception as exc:
        logger.error("Unhandled error in poll_roborock: %s", exc)


# ---------------------------------------------------------------------------
# Scheduler singleton
# ---------------------------------------------------------------------------

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def start_scheduler() -> None:
    """Register polling jobs and start the scheduler."""
    settings = get_settings()
    scheduler = get_scheduler()

    # ── Primary: multi-user poll ───────────────────────────────────────────────
    scheduler.add_job(
        poll_all_users,
        trigger=IntervalTrigger(seconds=settings.poll_interval_smarthq),
        id="poll_all_users",
        name="Poll all user SmartHQ sessions",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "Registered multi-user poll job (every %d s).", settings.poll_interval_smarthq
    )

    # ── Legacy: single-user SmartHQ (only runs if credentials configured) ──────
    scheduler.add_job(
        poll_smarthq,
        trigger=IntervalTrigger(seconds=settings.poll_interval_smarthq),
        id="poll_smarthq",
        name="Poll SmartHQ appliances (legacy)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "Registered legacy SmartHQ polling job (every %d s).", settings.poll_interval_smarthq
    )

    # ── Legacy: Roborock ───────────────────────────────────────────────────────
    scheduler.add_job(
        poll_roborock,
        trigger=IntervalTrigger(seconds=settings.poll_interval_roborock),
        id="poll_roborock",
        name="Poll Roborock vacuums (legacy)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "Registered Roborock polling job (every %d s).", settings.poll_interval_roborock
    )

    scheduler.start()
    logger.info("APScheduler started.")


def stop_scheduler() -> None:
    """Gracefully stop the scheduler."""
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")
