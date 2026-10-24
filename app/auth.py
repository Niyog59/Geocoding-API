"""API keys and administrator authentication.

Keys look like `gk_<id>_<secret>`. Only a SHA-256 hash of the full key is
stored, so a copy of the database does not reveal usable keys. SHA-256 (rather
than a slow password hash) is appropriate here because the secret part is 32
random bytes, which cannot be guessed. The public `<id>` part lets an
administrator list and revoke a key without ever seeing the key itself.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

import redis.asyncio as redis
from fastapi import Request

from app.errors import ApiError

KEY_PATTERN = re.compile(r"^gk_([0-9a-f]{12})_[A-Za-z0-9_-]{43}$")

_KEY_RECORD = "geo:key:{hash}"  # hash of the full key -> key record
_KEY_ID = "geo:keyid:{id}"  # public key id -> hash of the full key
_KEY_IDS = "geo:keys"  # set of all key ids


@dataclass(frozen=True)
class KeyRecord:
    id: str
    name: str
    rate_per_minute: int
    daily_limit: int
    created_at: str
    revoked: bool

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "rate_per_minute": self.rate_per_minute,
            "daily_limit": self.daily_limit,
            "created_at": self.created_at,
            "revoked": self.revoked,
        }


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_key() -> tuple[str, str]:
    key_id = secrets.token_hex(6)
    return key_id, f"gk_{key_id}_{secrets.token_urlsafe(32)}"


def _record_from(data: dict[str, str]) -> KeyRecord:
    return KeyRecord(
        id=data["id"],
        name=data["name"],
        rate_per_minute=int(data["rate_per_minute"]),
        daily_limit=int(data["daily_limit"]),
        created_at=data["created_at"],
        revoked=data.get("revoked") == "1",
    )


class KeyStore:
    """API key records kept in Redis, shared by every worker and replica."""

    def __init__(self, client: redis.Redis) -> None:
        self.redis = client

    async def create(self, name: str, rate_per_minute: int, daily_limit: int) -> tuple[str, KeyRecord]:
        key_id, api_key = generate_key()
        key_hash = hash_key(api_key)
        record = KeyRecord(
            id=key_id,
            name=name,
            rate_per_minute=rate_per_minute,
            daily_limit=daily_limit,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            revoked=False,
        )
        mapping = {**{k: str(v) for k, v in record.public().items()}, "revoked": "0"}
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(_KEY_RECORD.format(hash=key_hash), mapping=mapping)
            pipe.set(_KEY_ID.format(id=key_id), key_hash)
            pipe.sadd(_KEY_IDS, key_id)
            await pipe.execute()
        return api_key, record

    async def lookup(self, api_key: str) -> KeyRecord | None:
        """Return the active record for a key, or None if it is unknown or revoked."""
        if not KEY_PATTERN.match(api_key):
            return None
        data = await self.redis.hgetall(_KEY_RECORD.format(hash=hash_key(api_key)))  # type: ignore[misc]
        if not data:
            return None
        record = _record_from(data)
        return None if record.revoked else record

    async def revoke(self, key_id: str) -> bool:
        key_hash = await self.redis.get(_KEY_ID.format(id=key_id))
        if not key_hash:
            return False
        await self.redis.hset(_KEY_RECORD.format(hash=key_hash), "revoked", "1")  # type: ignore[misc]
        return True

    async def list(self) -> list[KeyRecord]:
        records = []
        for key_id in sorted(await self.redis.smembers(_KEY_IDS)):  # type: ignore[misc]
            key_hash = await self.redis.get(_KEY_ID.format(id=key_id))
            data = await self.redis.hgetall(_KEY_RECORD.format(hash=key_hash)) if key_hash else {}  # type: ignore[misc]
            if data:
                records.append(_record_from(data))
        return records


def check_admin_secret(request: Request, configured: str | None) -> None:
    """Allow the request only if it carries the administrator secret header."""
    if not configured:
        raise ApiError(503, "admin_disabled", "Administration is not configured on this server.")
    supplied = request.headers.get("X-Admin-Secret", "")
    if not hmac.compare_digest(supplied.encode(), configured.encode()):
        raise ApiError(401, "invalid_admin_secret", "A valid X-Admin-Secret header is required.")
