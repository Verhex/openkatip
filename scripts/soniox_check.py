"""Toplantıdan önce Soniox ön kontrolü — kayıt dosyası gerekmez.

1. Anahtar ve bütçe çalışıyor mu (TTS model listesi alınır).
2. Türkçe bir metin sese çevrilir → data/soniox-ornek.wav (dinleyip telaffuzu kontrol edin).
3. Aynı ses gerçek zamanlı hızda STT'ye verilir: Türkçe transkript + İngilizce çeviri + gecikme.

Kullanım:
    python scripts/soniox_check.py
    python scripts/soniox_check.py --voice Maya --text "Merhaba, ben Katip."
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np

from katip.config import get_settings
from katip.pipeline.stt.soniox import SonioxSTT
from katip.voice.tts import SonioxTTS, apply_lexicon

DEFAULT_TEXT = (
    "Merhaba, ben Katip. Bugünkü toplantıda bütçe, lansman tarihi ve müşteri "
    "görüşmelerini konuşacağız. Ayşe Hanım çarşamba gününe kadar teklifi hazırlayacak; "
    "Deniz Bey de ölçüm sonuçlarını paylaşacak."
)


def list_tts_models(api_key: str) -> None:
    r = httpx.get(
        "https://api.soniox.com/v1/tts-models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"  ! model listesi alınamadı: HTTP {r.status_code} {r.text[:200]}")
        return
    for model in r.json().get("models", []):
        voices = [v.get("id") or v.get("name") for v in model.get("voices", [])]
        langs = model.get("languages") or []
        has_tr = any((lng.get("code") if isinstance(lng, dict) else lng) == "tr" for lng in langs)
        names = ", ".join(map(str, voices[:12]))
        print(f"  • {model.get('id')}  Türkçe: {'var' if has_tr else '?'}  sesler: {names}")


def resample_to_16k(pcm: bytes, rate: int) -> bytes:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if rate == 16000 or samples.size == 0:
        return pcm
    n = int(samples.size * 16000 / rate)
    out = np.interp(np.linspace(0, samples.size - 1, n), np.arange(samples.size), samples)
    return out.astype(np.int16).tobytes()


async def realtime(pcm16k: bytes, started: list[float]):
    step = 1024 * 2
    started.append(time.monotonic())
    for i, off in enumerate(range(0, len(pcm16k), step)):
        delay = started[0] + i * 1024 / 16000 - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        yield pcm16k[off : off + step]
    # Konuşma bittikten sonra kısa sessizlik: son parçanın kesinleşmesi için.
    for _ in range(30):
        await asyncio.sleep(1024 / 16000)
        yield b"\x00\x00" * 1024


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--voice", default=None)
    parser.add_argument("--target", default="en")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.soniox_api_key:
        print("SONIOX_API_KEY tanımlı değil (.env dosyasına ekleyin).")
        return 1
    if args.voice:
        settings.tts_voice = args.voice

    print("1) TTS modelleri")
    list_tts_models(settings.soniox_api_key)

    print(f"\n2) Metin sese çevriliyor (ses: {settings.tts_voice})")
    tts = SonioxTTS()
    t0 = time.monotonic()
    first = None
    chunks = []
    async for chunk in tts.synthesize(
        apply_lexicon(args.text, settings.tts_lexicon), language="tr"
    ):
        first = first or time.monotonic()
        chunks.append(chunk)
    pcm = b"".join(chunks)
    out = Path(settings.data_dir) / "soniox-ornek.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(tts.sample_rate)
        w.writeframes(pcm)
    duration = len(pcm) / 2 / tts.sample_rate
    print(f"  ilk ses: {(first - t0) * 1000:.0f} ms · süre: {duration:.1f} sn · dosya: {out}")

    print(f"\n3) Ses tekrar yazıya dökülüyor (tr → {args.target})")
    started: list[float] = []
    stt = SonioxSTT()
    async for utt in stt.stream(
        realtime(resample_to_16k(pcm, tts.sample_rate), started),
        language="tr",
        target_language=args.target,
    ):
        if utt.is_final:
            lag = time.monotonic() - (started[0] + utt.end_s)
            print(f"  [{utt.speaker_label or '?'}] {utt.text}")
            if utt.translation:
                print(f"      → {utt.translation}")
            print(f"      (gecikme {lag * 1000:.0f} ms)")
    print(
        "\nTamam. Metinle transkripti karşılaştırın; WAV dosyasını dinleyip telaffuzu kontrol edin."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
