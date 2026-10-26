"""Deepgram streaming STT adaptörü — yalnız açık seçimle (ham WebSocket, SDK yok).

Deepgram çeviri vermez; `translates = False` olduğu için pipeline çeviriyi Claude ile yapar.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import websockets

from katip.async_utils import wait_tasks
from katip.config import Settings, get_settings
from katip.logging import get_logger
from katip.pipeline.stt.base import STTProvider, Utterance
from katip.providers.interfaces import Capabilities

log = get_logger(__name__)

_ENDPOINT = "wss://api.deepgram.com/v1/listen"


class DeepgramSTT(STTProvider):
    name = "deepgram"
    region = "us"
    capabilities = Capabilities()

    @property
    def model(self):
        return self._model

    def __init__(self, model: str = "nova-3", *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._model = model
        if not self._settings.deepgram_api_key:
            raise RuntimeError("DEEPGRAM_API_KEY tanımlı değil")

    def _url(self, language: str) -> str:
        params = {
            "model": self._model,
            "language": language,
            "encoding": "linear16",
            "sample_rate": str(self._settings.sample_rate),
            "channels": "1",
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
            # Konuşma sonunu erken kesinleştirip çeviri gecikmesini düşürür.
            "endpointing": "300",
        }
        return f"{_ENDPOINT}?{urlencode(params)}"

    async def stream(
        self,
        audio: AsyncIterator[bytes],
        *,
        language: str,
        target_language: str | None = None,
    ) -> AsyncIterator[Utterance]:
        headers = {"Authorization": f"Token {self._settings.deepgram_api_key}"}
        async with websockets.connect(
            self._url(language), additional_headers=headers, max_size=None
        ) as ws:

            async def pump() -> None:
                try:
                    async for chunk in audio:
                        await ws.send(chunk)
                finally:
                    # Deepgram'a akışın bittiğini bildir; bekleyen final'ler gelsin.
                    await ws.send(json.dumps({"type": "CloseStream"}))

            pump_task = asyncio.create_task(pump())
            utt_id = uuid.uuid4().hex[:12]
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "Results":
                        continue
                    alt = (msg.get("channel", {}).get("alternatives") or [{}])[0]
                    text = (alt.get("transcript") or "").strip()
                    if not text:
                        continue
                    start = float(msg.get("start", 0.0))
                    is_final = bool(msg.get("is_final"))
                    yield Utterance(
                        id=utt_id,
                        text=text,
                        is_final=is_final,
                        start_s=start,
                        end_s=start + float(msg.get("duration", 0.0)),
                        lang=language,
                    )
                    if is_final:
                        utt_id = uuid.uuid4().hex[:12]
            finally:
                await wait_tasks({pump_task}, budget_s=1, label="Deepgram göndericisi", cancel=True)
