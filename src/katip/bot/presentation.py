from __future__ import annotations

import asyncio
import base64
import time

from katip.bot.raster import MAX_BYTES, render_page

CHUNK_BYTES = 32 * 1024


def transfer_messages(data: bytes, request_id: str) -> list[dict]:
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("Sunum 8 MB sınırını aşıyor.")
    return [
        {
            "kind": "presentation_data",
            "request_id": request_id,
            "offset": offset,
            "size": len(data),
            "data": base64.b64encode(data[offset : offset + CHUNK_BYTES]).decode(),
        }
        for offset in range(0, len(data), CHUNK_BYTES)
    ]


class Presentation:
    def __init__(self, page, platform, send, diagnose):
        self.page = page
        self.platform = platform
        self.send = send
        self.diagnose = diagnose
        self.data = b""
        self.pending = bytearray()
        self.transfer_id = ""
        self.transfer_at = 0.0
        self.transfer_size = 0
        self.current = 0
        self.pages = 0
        self.cache: dict[int, str] = {}
        self.lock = asyncio.Lock()

    def receive(self, msg):
        if msg.get("offset") == 0:
            self.pending.clear()
            self.transfer_id = msg.get("request_id")
            self.transfer_at = time.monotonic()
            self.transfer_size = msg.get("size")
        if (
            msg.get("request_id") != self.transfer_id
            or type(msg.get("size")) is not int
            or not 0 < msg["size"] <= MAX_BYTES
            or msg["size"] != self.transfer_size
            or msg.get("offset") != len(self.pending)
            or time.monotonic() - self.transfer_at > 30
        ):
            self.pending.clear()
            raise ValueError("Sunum aktarımı geçersiz; yeniden başlatın.")
        chunk = base64.b64decode(msg.get("data", ""), validate=True)
        if len(chunk) > CHUNK_BYTES or len(self.pending) + len(chunk) > msg["size"]:
            self.pending.clear()
            raise ValueError("Sunum aktarımı boyut sınırını aşıyor.")
        self.pending.extend(chunk)

    async def _show(self, number):
        self.cache = {n: img for n, img in self.cache.items() if abs(n - number) <= 1}
        if number not in self.cache:
            result = await render_page(self.data, number)
            self.pages = result["pages"]
            self.cache[number] = result["image"]
        shown = 0
        for frame in self.page.frames:
            from katip.bot.runner import _trusted_frame

            if _trusted_frame(frame, self.platform):
                shown += bool(
                    await frame.evaluate(
                        "async url => { if (!window.__katipScreen) return false; "
                        "await window.__katipScreen.show(url); return true; }",
                        self.cache[number],
                    )
                )
        if not shown:
            raise ValueError("Toplantı ekranına erişilemedi.")
        self.current = number

    async def _stop(self):
        for frame in self.page.frames:
            try:
                await frame.evaluate("() => window.__katipScreen?.stop()")
            except Exception:
                continue
        self.data = b""
        self.pending.clear()
        self.cache.clear()
        self.current = 0
        self.pages = 0
        await self.platform.stop_share(self.page)

    async def handle(self, msg):
        async with self.lock:
            request_id = msg.get("request_id")
            action = msg.get("action")
            error = None
            try:
                if action == "stop":
                    await self._stop()
                elif action == "start":
                    if (
                        request_id != self.transfer_id
                        or len(self.pending) != msg.get("size")
                        or msg.get("size") != self.transfer_size
                    ):
                        raise ValueError("PDF aktarımı tamamlanmadı; yeniden başlatın.")
                    data = bytes(self.pending)
                    await self._stop()
                    self.data = data
                    await self._show(1)
                    await self.platform.start_share(self.page)
                else:
                    if not self.current:
                        raise ValueError("Önce sunumu başlatın.")
                    number = {"next": self.current + 1, "prev": self.current - 1}.get(
                        action, msg.get("page") if action == "goto" else None
                    )
                    if type(number) is not int or not 1 <= number <= self.pages:
                        raise ValueError(f"Slayt numarası 1–{self.pages} arasında olmalı.")
                    await self._show(number)
            except Exception as exc:
                error = (
                    str(exc) if isinstance(exc, ValueError) else "Ekran paylaşımı başlatılamadı."
                )
                if action == "start":
                    await self._stop()
                    await self.diagnose()
            await self.send(
                {
                    "kind": "presentation_status",
                    "request_id": request_id,
                    "page": self.current,
                    "pages": self.pages,
                    "error": error,
                }
            )

    async def close(self):
        async with self.lock:
            await self._stop()
