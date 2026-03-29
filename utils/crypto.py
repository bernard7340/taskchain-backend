"""Credential encryption utility.

Wraps ``cryptography.fernet`` to provide a simple encrypt/decrypt API for
storing SmartHQ passwords in memory.  The symmetric key is loaded from the
``ENCRYPTION_KEY`` environment variable (set in ``.env`` or Railway env vars).

Generate a fresh key with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Key bootstrap
# ---------------------------------------------------------------------------

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """Return (and lazily initialise) the Fernet instance.

    On first call the key is read from ``ENCRYPTION_KEY``.  If the variable is
    absent a fresh key is generated and logged as a warning so the operator
    knows to persist it.
    """
    global _fernet
    if _fernet is not None:
        return _fernet

    raw_key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if raw_key:
        try:
            _fernet = Fernet(raw_key.encode())
            logger.debug("Fernet key loaded from ENCRYPTION_KEY environment variable.")
            return _fernet
        except Exception as exc:
            logger.error(
                "ENCRYPTION_KEY is set but invalid (%s). Generating a temporary key. "
                "Credentials will not survive a restart.",
                exc,
            )

    # Fallback: generate an ephemeral key (in-memory only).
    ephemeral_key = Fernet.generate_key()
    _fernet = Fernet(ephemeral_key)
    logger.warning(
        "No valid ENCRYPTION_KEY found. Using an ephemeral Fernet key. "
        "Set ENCRYPTION_KEY in your environment to persist credentials across restarts. "
        "Generate one with: python -c \"from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())\""
    )
    return _fernet


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def encrypt(plaintext: str) -> bytes:
    """Encrypt *plaintext* and return the Fernet token as bytes."""
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes) -> str:
    """Decrypt a Fernet *token* and return the plaintext string.

    Raises ``InvalidToken`` if the token is corrupt or was encrypted with a
    different key.
    """
    return _get_fernet().decrypt(token).decode("utf-8")


def decrypt_safe(token: bytes) -> str | None:
    """Like ``decrypt`` but returns ``None`` instead of raising on failure."""
    try:
        return decrypt(token)
    except InvalidToken:
        logger.error("Failed to decrypt credential — token is invalid or key has rotated.")
        return None
