"""Türkçe için Soniox TTS ses karşılaştırması.

Her aday ses aynı Türkçe paragrafı okur (normalize edilmiş metinle). Ses, Soniox STT'ye
geri dinletilir ve anlaşılırlık (kelime doğruluğu) ölçülür. Ayrıca iki ses için normalize
edilmemiş metin de seslendirilir: "virgül sıfır sıfır" farkını kulakla duymak için.
Çıktı: data/tts-bench/*.wav + ekrana tablo.

Kullanım:
    python scripts/tts_voice_bench.py
    python scripts/tts_voice_bench.py --voices Iris,Andrei,Freya
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np

from katip.config import get_settings
from katip.pipeline.stt.soniox import SonioxSTT
from katip.voice.tr_normalize import normalize_tr
from katip.voice.tts import SonioxTTS

CANDIDATES = [
    # Kadın
    "Freya",
    "Iris",
    "Colleen",
    "Harper",
    "Rania",
    "Layla",
    # Erkek
    "Andrei",
    "Adrian",
    "Harlan",
    "Bohdan",
    "Manav",
    "Shane",
]

TEXT = (
    "Merhaba, ben Katip. 42.500,00 ₺ tutarındaki faturanızın son ödeme tarihi 15.10.2026. "
    "Toplantı saat 14:30'da başlayacak; katılım oranı %87,5 oldu. Deniz Bey, Selin Hanım ve "
    "Ayşe Yılmaz, üçüncü çeyrek ölçüm sonuçlarını çarşamba günü paylaşacak."
)


def words(text: str) -> list[str]:
    text = normalize_tr(text).replace("İ", "i").replace("I", "ı").lower()
    return re.findall(r"[a-zçğıöşü]+", text)


def word_accuracy(ref: str, hyp: str) -> float:
    """1 - WER (kelime düzeyinde Levenshtein)."""
    r, h = words(ref), words(hyp)
    d = np.zeros((len(r) + 1, len(h) + 1), dtype=int)
    d[:, 0] = range(len(r) + 1)
    d[0, :] = range(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            cost = 0 if r[i - 1] == h[j - 1] else 1
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + cost)
    return max(0.0, 1 - d[len(r), len(h)] / max(1, len(r)))


def to_16k(pcm: bytes, rate: int) -> bytes:
    s = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = int(s.size * 16000 / rate)
    return np.interp(np.linspace(0, s.size - 1, n), np.arange(s.size), s).astype(np.int16).tobytes()


async def chunks(pcm: bytes):
    for off in range(0, len(pcm), 3200):
        yield pcm[off : off + 3200]
        await asyncio.sleep(0.02)  # gerçek zamanın ~5 katı hız
    for _ in range(40):
        yield b"\x00\x00" * 1600
        await asyncio.sleep(0.01)


async def synth(voice: str, text: str) -> tuple[bytes, float]:
    settings = get_settings()
    settings.tts_voice = voice
    tts = SonioxTTS()
    t0 = time.monotonic()
    first = None
    parts = []
    async for part in tts.synthesize(text, language="tr"):
        first = first or time.monotonic()
        parts.append(part)
    return b"".join(parts), (first or t0) - t0


def save(path: Path, pcm: bytes, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


async def transcribe(pcm16k: bytes) -> str:
    out = []
    async for utt in SonioxSTT().stream(chunks(pcm16k), language="tr"):
        if utt.is_final:
            out.append(utt.text)
    return " ".join(out)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voices", default=",".join(CANDIDATES))
    args = parser.parse_args()
    voices = [v.strip() for v in args.voices.split(",") if v.strip()]
    settings = get_settings()
    out_dir = Path(settings.data_dir) / "tts-bench"
    rate = settings.tts_sample_rate
    spoken = normalize_tr(TEXT)
    print(f"Normalize metin: {spoken}\n")

    rows = []
    for voice in voices:
        try:
            pcm, first = await synth(voice, spoken)
        except Exception as exc:
            print(f"{voice:10} HATA: {exc}")
            continue
        save(out_dir / f"{voice}.wav", pcm, rate)
        hyp = await transcribe(to_16k(pcm, rate))
        acc = word_accuracy(spoken, hyp)
        rows.append((acc, voice, first, len(pcm) / 2 / rate, hyp))
        duration = len(pcm) / 2 / rate
        print(f"{voice:10} %{acc * 100:5.1f} · ilk ses {first * 1000:4.0f} ms · {duration:4.1f} sn")

    # Normalizasyon farkı: aynı sesle ham metin
    for voice in voices[:2]:
        pcm, _ = await synth(voice, TEXT)
        save(out_dir / f"{voice}-ham-metin.wav", pcm, rate)

    print("\nSıralama (anlaşılırlık):")
    for acc, voice, _first, _dur, hyp in sorted(rows, reverse=True):
        print(f"  %{acc * 100:5.1f}  {voice:10} STT: {hyp[:110]}")
    print(f"\nDosyalar: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
