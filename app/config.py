"""Configuration read from environment variables.

In production the service refuses to start with unsafe settings instead of
silently falling back to defaults.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_MARKERS = ("change-me", "changeme", "example", "replace")


class ConfigError(ValueError):
    """Raised when the configuration is unsafe or incomplete."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    redis_url: str = "redis://localhost:6379/0"

    # Access control
    admin_secret: str | None = None
    require_api_key: bool = True
    cors_origins: str = ""

    # Limits applied to new API keys unless the administrator sets others
    default_key_rate_per_minute: int = Field(60, ge=1)
    default_key_daily_limit: int = Field(1000, ge=1)
    max_key_rate_per_minute: int = Field(600, ge=1)
    max_key_daily_limit: int = Field(100_000, ge=1)

    # Anonymous callers of the keyed routes (only when REQUIRE_API_KEY=false)
    anon_rate_per_minute: int = Field(30, ge=1)
    anon_daily_limit: int = Field(500, ge=1)

    # Public demo routes used by the playground. Every limit is enforced on the server.
    demo_enabled: bool = True
    demo_rate_per_minute: int = Field(10, ge=1)
    demo_daily_per_ip: int = Field(100, ge=1)
    demo_daily_total: int = Field(1000, ge=1)
    demo_max_results: int = Field(5, ge=1, le=10)

    # Upstream budget shared by every caller, worker and replica. Keep it below
    # the provider account's real quota to leave room for testing.
    provider_daily_budget: int = Field(2000, ge=1)
    provider_rate_per_second: int = Field(4, ge=1)
    provider_wait_seconds: float = Field(2.0, ge=0)

    usage_retention_days: int = Field(35, ge=1)

    # Client IP for anonymous limits: "peer" uses the TCP connection address;
    # "x-forwarded-for" trusts that header, but only the entry added by the
    # outermost of TRUSTED_PROXY_COUNT proxies.
    client_ip_source: Literal["peer", "x-forwarded-for"] = "peer"
    trusted_proxy_count: int = Field(1, ge=1)

    # Upstream provider
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    contact_email: str = "rentpasainc@gmail.com"

    # Shared response cache
    cache_ttl_seconds: int = Field(86_400, ge=1)
    empty_cache_ttl_seconds: int = Field(3_600, ge=1)
    cache_max_entries: int = Field(20_000, ge=1)
    cache_max_entry_bytes: int = Field(64_000, ge=1)
    coalesce_lock_seconds: float = Field(15.0, gt=0)
    coalesce_wait_seconds: float = Field(10.0, ge=0)

    @property
    def user_agent(self) -> str:
        return f"GeocodingAPI/1.0 ({self.contact_email})"

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def _check_safety(self) -> Settings:
        problems: list[str] = []
        if "*" in self.origins:
            problems.append("CORS_ORIGINS must list exact origins; '*' is not allowed.")
        if self.default_key_rate_per_minute > self.max_key_rate_per_minute:
            problems.append("DEFAULT_KEY_RATE_PER_MINUTE exceeds MAX_KEY_RATE_PER_MINUTE.")
        if self.default_key_daily_limit > self.max_key_daily_limit:
            problems.append("DEFAULT_KEY_DAILY_LIMIT exceeds MAX_KEY_DAILY_LIMIT.")
        if self.app_env == "production":
            secret = self.admin_secret or ""
            if len(secret) < 32 or any(marker in secret.lower() for marker in PLACEHOLDER_MARKERS):
                problems.append("ADMIN_SECRET must be set to a random value of at least 32 characters.")
            if not self.redis_url.startswith(("redis://", "rediss://")):
                problems.append("REDIS_URL must be a redis:// or rediss:// URL.")
            if self.redis_url.startswith("redis://localhost") or self.redis_url.startswith("redis://127.0.0.1"):
                problems.append("REDIS_URL points at localhost, which is not shared or durable in production.")
        if problems:
            raise ConfigError(" ".join(problems))
        return self
