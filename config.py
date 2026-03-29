from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── SmartHQ (legacy single-user — leave blank for cloud multi-user mode) ──
    smarthq_api_key: str = Field(default="", description="SmartHQ bearer token")
    smarthq_username: str = Field(default="", description="SmartHQ account email")
    smarthq_password: str = Field(default="", description="SmartHQ account password")
    smarthq_base_url: str = Field(
        default="https://api.smarthq.com/v1",
        description="SmartHQ API base URL",
    )

    # ── Roborock (legacy single-user) ─────────────────────────────────────────
    roborock_username: str = Field(default="", description="Roborock account email")
    roborock_password: str = Field(default="", description="Roborock account password")

    # ── Firebase ──────────────────────────────────────────────────────────────
    firebase_credentials_path: str = Field(
        default="./firebase-credentials.json",
        description="Path to Firebase service account JSON",
    )
    fcm_default_token: str = Field(
        default="", description="Default FCM device token (legacy single-user)"
    )

    # ── Credential encryption ─────────────────────────────────────────────────
    encryption_key: str = Field(
        default="",
        description=(
            "Fernet symmetric key for encrypting SmartHQ passwords in memory. "
            "Generate with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        ),
    )

    # ── Polling intervals (seconds) ───────────────────────────────────────────
    poll_interval_smarthq: int = Field(default=30, ge=5)
    poll_interval_roborock: int = Field(default=15, ge=5)

    # ── Server ────────────────────────────────────────────────────────────────
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000, ge=1, le=65535)

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins: list[str] = Field(
        default=["http://localhost", "http://10.0.2.2", "http://10.0.2.2:8000"],
        description="Allowed CORS origins — add your Railway URL after deploying",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
