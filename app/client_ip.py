"""Working out which client made a request.

Forwarded-for headers are only trusted when the deployment says a known number
of proxies sit in front of the app. Otherwise anyone could send a fake header
and get a fresh rate-limit bucket on every request.
"""

from __future__ import annotations

from fastapi import Request

from app.config import Settings


def client_ip(request: Request, settings: Settings) -> str:
    peer = request.client.host if request.client else "unknown"
    if settings.client_ip_source != "x-forwarded-for":
        return peer
    header = request.headers.get("X-Forwarded-For", "")
    hops = [part.strip() for part in header.split(",") if part.strip()]
    # Each trusted proxy appends the address it received the request from, so
    # the client is the entry added by the outermost trusted proxy.
    if len(hops) >= settings.trusted_proxy_count:
        return hops[-settings.trusted_proxy_count]
    return peer
