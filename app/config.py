"""Configuration read from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    cache_ttl: int = 3600
    rate_limit: str = "30/minute"
    rate_limit_authenticated: str = "100/minute"
    contact_email: str = "rentpasainc@gmail.com"
    allowed_origins: str = "*"
    require_api_key: bool = False
    master_api_key: str = ""

    @property
    def user_agent(self) -> str:
        return f"GeocodingAPI/1.0 ({self.contact_email})"

    @property
    def origins(self) -> list[str]:
        return self.allowed_origins.split(",")
