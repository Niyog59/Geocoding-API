"""Per-key usage counters (in process memory; moved to Redis in a later change)."""

from __future__ import annotations

from datetime import datetime
from typing import Any


class UsageTracker:
    def __init__(self) -> None:
        self.usage: dict[str, dict[str, Any]] = {}

    def track(self, key_id: str | None, endpoint: str) -> None:
        if not key_id:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        day = self.usage.setdefault(key_id, {}).setdefault(today, {"total": 0, "endpoints": {}})
        day["total"] += 1
        day["endpoints"][endpoint] = day["endpoints"].get(endpoint, 0) + 1

    def get(self, key_id: str) -> dict[str, Any]:
        usage = self.usage.get(key_id, {})
        today = datetime.now().strftime("%Y-%m-%d")
        return {
            "today": usage.get(today, {"total": 0, "endpoints": {}}),
            "total_days": len(usage),
            "all_time_requests": sum(day.get("total", 0) for day in usage.values()),
        }
