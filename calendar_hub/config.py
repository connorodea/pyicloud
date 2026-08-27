"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Secrets may be supplied through mounted files so they never need to appear in
    source control, process arguments, or the public container configuration.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["development", "test", "production"] = "development"
    public_base_url: str = "http://localhost:8787"
    page_owner_name: str = "Connor O'Dea"
    owner_timezone: str = "America/Denver"
    owner_phone: str = ""

    availability_start: str = "10:30"
    availability_end: str = "20:00"
    availability_weekdays: str = "0,1,2,3,4"
    slot_increment_minutes: int = Field(default=15, ge=5, le=60)
    minimum_notice_hours: int = Field(default=12, ge=0, le=720)
    booking_window_days: int = Field(default=60, ge=1, le=365)
    buffer_before_minutes: int = Field(default=10, ge=0, le=120)
    buffer_after_minutes: int = Field(default=10, ge=0, le=120)

    video_duration_minutes: int = Field(default=30, ge=10, le=240)
    phone_duration_minutes: int = Field(default=20, ge=10, le=240)
    video_provider: Literal["zoom", "google_meet"] = "zoom"

    google_oauth_token_file: str = "/run/secrets/google-token.json"
    google_busy_calendar_ids: str = "primary"
    google_booking_calendar_id: str = "primary"

    pyicloud_enabled: bool = True
    pyicloud_apple_id: str = ""
    pyicloud_password_file: str = "/run/secrets/icloud-password"
    pyicloud_cookie_directory: str = "/data/pyicloud"
    pyicloud_china_mainland: bool = False

    ics_calendar_urls: str = ""
    ics_request_timeout_seconds: int = Field(default=10, ge=1, le=60)

    zoom_account_id: str = ""
    zoom_client_id: str = ""
    zoom_client_secret_file: str = "/run/secrets/zoom-client-secret"
    zoom_user_id: str = ""

    redis_url: str = "redis://redis:6379/0"
    turnstile_site_key: str = ""
    turnstile_secret_file: str = ""
    turnstile_required: bool = False
    rate_limit_per_minute: int = Field(default=30, ge=1, le=1000)
    allowed_hosts: str = "schedule.connorodea.com,localhost,127.0.0.1,testserver"
    trust_proxy_headers: bool = False

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("public_base_url must start with http:// or https://")
        return value.rstrip("/")

    @property
    def busy_calendar_ids(self) -> list[str]:
        return _split_csv(self.google_busy_calendar_ids)

    @property
    def weekday_numbers(self) -> set[int]:
        values = {int(value) for value in _split_csv(self.availability_weekdays)}
        if not values or not values.issubset(set(range(7))):
            raise ValueError("availability_weekdays must contain values from 0 to 6")
        return values

    @property
    def calendar_feed_urls(self) -> list[str]:
        return _split_csv(self.ics_calendar_urls)

    @property
    def host_allowlist(self) -> list[str]:
        return _split_csv(self.allowed_hosts)

    def read_secret(self, path: str, *, required: bool = True) -> str:
        if not path:
            if required:
                raise RuntimeError("A required secret file path is not configured")
            return ""
        secret_path = Path(path)
        try:
            value = secret_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            if required:
                raise RuntimeError(
                    f"Unable to read required secret file: {path}"
                ) from exc
            return ""
        if required and not value:
            raise RuntimeError(f"Required secret file is empty: {path}")
        return value


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
