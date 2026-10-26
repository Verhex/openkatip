"""Claude ile bağlam duyarlı çeviri — yalnızca çeviri vermeyen STT sağlayıcıları için yedek.

Soniox çeviriyi akışın içinde verdiği için birincil yolda bu modül kullanılmaz;
Deepgram gibi sağlayıcılarda kesinleşen segmentler buradan çevrilir. Her istek,
son N segmentin kaynak ve hedef metnini bağlam olarak taşır.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

from anthropic import AsyncAnthropic

from katip import usage
from katip.agent.llm import supports_effort, thinking_off
from katip.config import get_settings
from katip.logging import get_logger
from katip.providers import get_registry

log = get_logger(__name__)

_CONTEXT_TURNS = 6

_SYSTEM = """Sen canlı bir toplantıda çalışan simultane çevirmensin.
Kaynak dildeki konuşma parçasını hedef dile çevirirsin.

Kurallar:
- SADECE çeviriyi yaz. Açıklama, not, tırnak, önek ekleme.
- Konuşma dilinin tonunu koru; resmî bir metne dönüştürme.
- Yarım kalmış cümleyi tamamlama, olduğu gibi çevir.
- Özel isimleri, şirket ve ürün adlarını çevirme.
- Verilen sözlükteki terimleri birebir o karşılıkla kullan.
- Parça çevrilemeyecek kadar anlamsızsa (dolgu sesi, tek hece) boş satır döndür.
"""


@dataclass(slots=True)
class _Turn:
    source: str
    target: str


class Translator:
    """Oturum başına bir örnek; kendi bağlam penceresini taşır."""

    def __init__(
        self,
        source_lang: str | None = None,
        target_lang: str | None = None,
        glossary: dict[str, str] | None = None,
    ) -> None:
        settings = get_settings()
        self._client = get_registry().llm("translation", anthropic_factory=AsyncAnthropic)
        self._model = settings.agent_model
        self.source_lang = source_lang or settings.source_lang
        self.target_lang = target_lang or settings.target_lang
        self._glossary = glossary or {}
        self._history: deque[_Turn] = deque(maxlen=_CONTEXT_TURNS)

    def _system_blocks(self) -> list[dict]:
        text = f"{_SYSTEM}\nKaynak dil: {self.source_lang}\nHedef dil: {self.target_lang}\n"
        if self._glossary:
            terms = "\n".join(f"- {k} -> {v}" for k, v in self._glossary.items())
            text += f"\nSözlük:\n{terms}\n"
        return [{"type": "text", "text": text}]

    def _messages(self, segment_text: str) -> list[dict]:
        messages: list[dict] = []
        for turn in self._history:
            messages.append({"role": "user", "content": turn.source})
            messages.append({"role": "assistant", "content": turn.target})
        messages.append({"role": "user", "content": segment_text})
        return messages

    async def translate(self, segment_text: str) -> str:
        """Tek bir segmenti çevirir ve bağlam penceresine ekler."""
        chunks: list[str] = []
        async for piece in self.translate_stream(segment_text):
            chunks.append(piece)
        out = "".join(chunks).strip()
        # Kesilen akışın yarım çevirisi kesinleşmiş segment olarak saklanmaz.
        return (
            out
            if any(t.source == segment_text.strip() and t.target == out for t in self._history)
            else ""
        )

    async def translate_stream(self, segment_text: str) -> AsyncIterator[str]:
        """Çeviriyi parça parça yayınlar — panelde yazılıyormuş gibi görünsün diye."""
        text = segment_text.strip()
        if not text:
            return

        collected: list[str] = []
        # Çeviri gecikmeye duyarlı; düşük efor, düşünme kapalı.
        args = thinking_off(self._model)
        if supports_effort(self._model):
            args["output_config"] = {"effort": "low"}
        try:
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=1024,
                **args,
                system=self._system_blocks(),
                messages=self._messages(text),
            ) as stream:
                async for piece in stream.text_stream:
                    collected.append(piece)
                    yield piece
                if getattr(stream, "get_final_message", None) is not None:
                    response = await stream.get_final_message()
                    if response is not None:
                        await usage.record_llm(
                            "çeviri", self._model, getattr(response, "usage", None)
                        )
        except Exception as exc:  # çeviri düşse bile transkript akmaya devam etmeli
            log.warning("çeviri başarısız", error_type=type(exc).__name__)
            return

        out = "".join(collected).strip()
        if out:
            self._history.append(_Turn(source=text, target=out))
