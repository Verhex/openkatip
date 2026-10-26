from __future__ import annotations

import asyncio
import hashlib
import time

import websockets

from katip.api import security
from katip.bot.runner import ServerLink


async def test_link_closes_browser_after_missing_api_health_ack():
    closed = asyncio.Event()

    async def on_message(message):
        pass

    async def dead():
        closed.set()

    link = ServerLink("ws://127.0.0.1:1/ws/ingest/test", on_message, dead)
    link._last_ack = time.monotonic() - 1000
    await link._health()
    assert closed.is_set() and link._closed
    await link.close()


async def test_policy_rejection_exits_instead_of_reconnecting():
    connections = []
    closed = asyncio.Event()

    async def api(ws):
        connections.append(ws)
        await ws.close(code=1008)

    async def on_message(message):
        pass

    async def dead():
        closed.set()

    async with websockets.serve(api, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        link = ServerLink(f"ws://127.0.0.1:{port}/ws/ingest/test", on_message, dead)
        await link.start()
        await asyncio.wait_for(closed.wait(), 3)
        await link.close()
    assert len(connections) == 1


async def test_offline_commands_and_audio_are_not_replayed():
    async def on_message(message):
        pass

    link = ServerLink("ws://127.0.0.1:1", on_message)
    link._connected_once = True
    await link.send_json({"kind": "chat", "text": "/not eski komut"})
    await link.send_json({"kind": "status", "state": "active"})
    await link.send_json({"kind": "roster", "names": ["Ayşe"]})
    await link.send_audio(b"audio")
    assert not link._pending
    assert set(link._snapshot) == {"status", "roster"}
    await link.close()


def test_expired_remote_token_is_denied_before_health_sweeper_runs():
    token = "remote-test-token"
    security.register_bot_token_hash(
        "test", hashlib.sha256(token.encode()).hexdigest(), expires=time.time() - 1
    )
    assert not security.check_bot_token("test", token)
    security.revoke_bot_token("test")
