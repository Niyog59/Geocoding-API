"""Calls to a Nominatim server."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import HTTPException

logger = logging.getLogger("app")


class NominatimClient:
    def __init__(self, base_url: str, user_agent: str) -> None:
        self.base_url = base_url
        self.user_agent = user_agent

    async def request(self, endpoint: str, params: dict[str, Any], request_id: str = "") -> Any:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        url = f"{self.base_url}/{endpoint}"
        try:
            async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
                response = await client.get(url, params=params)
                if response.status_code == 403:
                    logger.error(f"[{request_id}] Nominatim 403 Forbidden")
                    raise HTTPException(status_code=503, detail="Geocoding service temporarily unavailable")
                if response.status_code == 429:
                    raise HTTPException(status_code=429, detail="Geocoding service rate limit exceeded")
                response.raise_for_status()
                return response.json()
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Geocoding service timeout") from None
        except httpx.HTTPStatusError:
            raise HTTPException(status_code=502, detail="Geocoding service error") from None
        except httpx.RequestError:
            raise HTTPException(status_code=502, detail="Failed to connect to geocoding service") from None
