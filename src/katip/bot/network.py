"""Browser HTTP/WebSocket guard; host firewall remains the isolation boundary."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


async def public_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme in {"about", "blob", "data"}:
        return True
    if parsed.scheme not in {"https", "http", "wss", "ws"} or not parsed.hostname:
        return False
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            records = await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM), 3
            )
            addresses = [ipaddress.ip_address(row[4][0].split("%", 1)[0]) for row in records]
        except (OSError, TimeoutError, ValueError):
            return False
    return bool(addresses) and all(
        a.is_global
        and not a.is_multicast
        and (not getattr(a, "ipv4_mapped", None) or a.ipv4_mapped.is_global)
        for a in addresses
    )
