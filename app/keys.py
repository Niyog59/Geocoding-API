"""API key storage and usage tracking (in process memory)."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime
from typing import Any

logger = logging.getLogger("app")


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


class KeyStore:
    def __init__(self, master_api_key: str) -> None:
        self.master_api_key = master_api_key
        self.keys: dict[str, dict[str, Any]] = {}
        self.usage: dict[str, dict[str, Any]] = {}

    def init_default_keys(self) -> None:
        demo_key = "geo_demo_key_for_testing_only"
        self.keys[hash_api_key(demo_key)] = {
            "name": "Demo Key",
            "created_at": datetime.now().isoformat(),
            "rate_limit": "60/minute",
            "active": True,
        }
        if self.master_api_key:
            self.keys[hash_api_key(self.master_api_key)] = {
                "name": "Master Key",
                "created_at": datetime.now().isoformat(),
                "rate_limit": "1000/minute",
                "active": True,
            }
        logger.info(f"Initialized {len(self.keys)} API keys")
        logger.info(f"Demo API key for testing: {demo_key}")

    def generate(self, name: str, rate_limit: str) -> str:
        new_key = f"geo_{secrets.token_urlsafe(32)}"
        self.keys[hash_api_key(new_key)] = {
            "name": name,
            "created_at": datetime.now().isoformat(),
            "rate_limit": rate_limit,
            "active": True,
        }
        return new_key

    def validate(self, api_key: str | None) -> dict[str, Any] | None:
        if not api_key:
            return None
        key_data = self.keys.get(hash_api_key(api_key))
        if key_data and key_data.get("active", False):
            return key_data
        return None

    def track_usage(self, api_key: str | None, endpoint: str) -> None:
        if not api_key:
            return
        key_hash = hash_api_key(api_key)
        today = datetime.now().strftime("%Y-%m-%d")
        day = self.usage.setdefault(key_hash, {}).setdefault(today, {"total": 0, "endpoints": {}})
        day["total"] += 1
        day["endpoints"][endpoint] = day["endpoints"].get(endpoint, 0) + 1

    def get_usage(self, api_key: str) -> dict[str, Any]:
        usage = self.usage.get(hash_api_key(api_key), {})
        today = datetime.now().strftime("%Y-%m-%d")
        return {
            "today": usage.get(today, {"total": 0, "endpoints": {}}),
            "total_days": len(usage),
            "all_time_requests": sum(day.get("total", 0) for day in usage.values()),
        }
