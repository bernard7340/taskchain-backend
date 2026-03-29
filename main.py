"""TaskChain FastAPI backend – entry point.

Multi-user cloud deployment
---------------------------
This backend is designed to run on Railway (or any cloud PaaS) and serve
multiple users simultaneously.  Each user authenticates with Firebase and
provides their own SmartHQ credentials via the /api/v1/users/register endpoint.

Startup sequence
----------------
1. Initialise Firebase Admin SDK (for token verification + FCM).
2. Start legacy SmartHQ / Roborock clients (if env credentials configured).
3. Launch APScheduler — includes multi-user poll_all_users() job.

Shutdown sequence
-----------------
1. Stop APScheduler.
2. Close all active user sessions (per-user SmartHQ clients).
3. Close legacy service clients.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from routers.appliances import fcm_router, router as appliances_router
from routers.alarms import router as alarms_router
from routers.webhooks import router as webhooks_router
from routers.users import router as users_router
from services import fcm_service
from services.smarthq_service import smarthq_service
from services.roborock_service import roborock_service
from services.user_session_manager import user_session_manager
from polling.scheduler import start_scheduler, stop_scheduler

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown of all background services."""
    logger.info("TaskChain backend starting up…")

    # 1. Firebase Admin SDK — required for both token verification and FCM.
    fcm_service.initialize_firebase()

    # Set encryption key from env so the crypto module picks it up.
    enc_key = get_settings().encryption_key
    if enc_key:
        os.environ.setdefault("ENCRYPTION_KEY", enc_key)

    # 2. Legacy SmartHQ client (only if single-user env credentials configured).
    try:
        await smarthq_service.start()
        if smarthq_service.is_ready:
            logger.info("Legacy SmartHQ service started.")
        else:
            logger.info(
                "Legacy SmartHQ: no credentials configured — skipping singleton start. "
                "Users will connect via POST /api/v1/users/register instead."
            )
    except Exception as exc:
        logger.warning("Legacy SmartHQ service failed to start: %s", exc)

    # 3. Legacy Roborock client.
    try:
        await roborock_service.start()
        if roborock_service.is_connected:
            logger.info("Roborock service started.")
        else:
            logger.info("Roborock: not configured or not connected.")
    except Exception as exc:
        logger.warning("Roborock service failed to start: %s", exc)

    # 4. Polling scheduler (includes multi-user poll_all_users job).
    start_scheduler()

    logger.info("TaskChain backend is ready.")
    yield  # --- application runs here ---

    # --- Shutdown ---
    logger.info("TaskChain backend shutting down…")

    stop_scheduler()

    # Close all active user sessions.
    sessions = user_session_manager.get_all_sessions()
    for session in sessions:
        try:
            await session.smarthq_client.stop()
        except Exception as exc:
            logger.warning("Error closing client for user=%s: %s", session.user_id, exc)
    logger.info("Closed %d active user session(s).", len(sessions))

    try:
        await roborock_service.stop()
    except Exception as exc:
        logger.error("Error stopping Roborock service: %s", exc)

    try:
        await smarthq_service.stop()
    except Exception as exc:
        logger.error("Error stopping legacy SmartHQ service: %s", exc)

    logger.info("TaskChain backend stopped.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

settings = get_settings()

app = FastAPI(
    title="TaskChain Backend",
    description=(
        "Multi-user cloud backend for the TaskChain Android app.  "
        "Tracks GE appliances via SmartHQ, manages per-user alarm arming, "
        "and delivers FCM push notifications.  "
        "Each user authenticates with Firebase and provides their own SmartHQ credentials."
    ),
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

API_PREFIX = "/api/v1"

# Multi-user routes (new)
app.include_router(users_router, prefix=API_PREFIX)

# Legacy single-user routes (preserved for backward compatibility)
app.include_router(appliances_router, prefix=API_PREFIX)
app.include_router(fcm_router, prefix=API_PREFIX)
app.include_router(alarms_router, prefix=API_PREFIX)
app.include_router(webhooks_router, prefix=API_PREFIX)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"], summary="Health check")
async def health_check() -> dict:
    """Returns 200 OK with service status."""
    return {
        "status": "ok",
        "version": "2.0.0",
        "active_users": user_session_manager.active_user_count(),
        "legacy_smarthq_connected": smarthq_service.is_ready,
        "legacy_roborock_connected": roborock_service.is_connected,
    }


# ---------------------------------------------------------------------------
# Dev-server entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
        log_level="info",
    )
