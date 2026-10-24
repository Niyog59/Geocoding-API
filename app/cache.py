"""Response cache (in process memory)."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any


def cache_key(prefix: str, params: dict[str, Any]) -> str:
    key_str = json.dumps(params, sort_keys=True)
    return f"{prefix}:{hashlib.sha256(key_str.encode()).hexdigest()[:16]}"


class MemoryCache:
    def __init__(self, ttl: int, max_size: int = 10000) -> None:
        self.ttl = ttl
        self.max_size = max_size
        self.entries: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Any:
        if key in self.entries:
            data, timestamp = self.entries[key]
            if time.time() - timestamp < self.ttl:
                return data
            del self.entries[key]
        return None

    def set(self, key: str, data: Any) -> None:
        if len(self.entries) >= self.max_size:
            oldest = sorted(self.entries.keys(), key=lambda k: self.entries[k][1])[:100]
            for old in oldest:
                del self.entries[old]
        self.entries[key] = (data, time.time())
