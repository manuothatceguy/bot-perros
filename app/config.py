from __future__ import annotations

from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tz: str = "America/Argentina/Buenos_Aires"
    waha_url: str = "http://waha:3000"
    waha_api_key: str = ""
    waha_session: str = "default"
    whatsapp_group_id: str = ""
    whatsapp_group_name: str = "Perros"
    webhook_public_url: str = "http://bot-perros:8000/webhook"

    lunch_time: str = "12:00"
    lunch_followup_time: str = "14:00"
    dinner_time: str = "20:00"
    dinner_followup_time: str = "22:00"

    db_path: str = "data/bot_perros.db"
    app_public_url: str = "http://perros.local"
    google_calendar_id: str = ""
    google_service_account_file: str = ""

    def get_tz(self) -> ZoneInfo:
        """Return configured ZoneInfo object."""
        return ZoneInfo(self.tz)
