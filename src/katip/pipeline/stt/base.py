"""STT sağlayıcı arayüzü.

Sağlayıcılar `Utterance` güncellemeleri yayınlar. Aynı konuşma parçası, sabit bir
`id` ile birden çok kez gelir: metni büyür, sonunda `is_final` olur; çeviri
destekleyen sağlayıcılarda `translation` alanı da aynı parçanın üstüne dolar.
Tüketici (pipeline/panel) bu yüzden her güncellemeyi id'ye göre *upsert* eder.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass


class StreamExpired(RuntimeError):
    """Sağlayıcının normal oturum süresi doldu; aynı sağlayıcıda yenilenir."""


@dataclass(slots=True)
class Utterance:
    id: str
    text: str
    is_final: bool
    # Zamanlar, bu STT akışının başlangıcına göredir (saniye).
    start_s: float
    end_s: float
    # Sağlayıcının diarization etiketi ("1", "2", ...). İsim eşlemesi pipeline'da yapılır.
    speaker_label: str | None = None
    lang: str | None = None
    translation: str = ""
    translation_lang: str | None = None


class STTProvider(abc.ABC):
    """Tek bir (karışık) toplantı ses akışını yazıya döker."""

    #: Sağlayıcı çeviriyi akışın içinde veriyor mu? Vermiyorsa pipeline kendisi çevirir.
    translates: bool = False

    @abc.abstractmethod
    def stream(
        self,
        audio: AsyncIterator[bytes],
        *,
        language: str,
        target_language: str | None = None,
    ) -> AsyncIterator[Utterance]:
        """16 kHz mono int16 PCM chunk'larını alır, `Utterance` güncellemeleri yayınlar."""


def build_provider() -> STTProvider:
    from katip.providers import get_registry

    return get_registry().stt()
