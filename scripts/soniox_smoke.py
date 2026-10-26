"""Soniox'u gerçek bir ses dosyasıyla, toplantıya bot sokmadan dene.

Dosyayı gerçek zamanlı hızda (16 kHz mono PCM'e çevirerek) akıtır; kısmi ve final
transkripti, çeviriyi, konuşmacı etiketini ve her final parçanın gecikmesini basar.
Gecikme = parça sesinin bittiği an ile final metnin geldiği an arasındaki süre.

Kullanım (ffmpeg gerekir):
    python scripts/soniox_smoke.py kayit.wav --target en
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from collections.abc import AsyncIterator

from katip.config import get_settings
from katip.pipeline.stt.soniox import SonioxSTT

CHUNK_SAMPLES = 1024  # capture.js ile aynı frame boyu


def decode_to_pcm(path: str, sample_rate: int) -> bytes:
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        path,
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-",
    ]
    return subprocess.run(cmd, check=True, capture_output=True).stdout


async def realtime(pcm: bytes, sample_rate: int, started: list[float]) -> AsyncIterator[bytes]:
    step = CHUNK_SAMPLES * 2
    chunk_s = CHUNK_SAMPLES / sample_rate
    started.append(time.monotonic())
    for i, offset in enumerate(range(0, len(pcm), step)):
        # Saat kaymasını önlemek için mutlak zamana göre bekle.
        delay = started[0] + i * chunk_s - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        yield pcm[offset : offset + step]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", help="Ses dosyası (wav/mp3/m4a/...)")
    parser.add_argument("--source", default="tr")
    parser.add_argument("--target", default="en")
    args = parser.parse_args()

    settings = get_settings()
    pcm = decode_to_pcm(args.audio, settings.sample_rate)
    print(f"{len(pcm) / 2 / settings.sample_rate:.1f} sn ses akıtılıyor...\n")

    started: list[float] = []
    latencies: list[float] = []
    printed: set[str] = set()
    stream = SonioxSTT().stream(
        realtime(pcm, settings.sample_rate, started),
        language=args.source,
        target_language=args.target,
    )
    async for utt in stream:
        if not utt.is_final:
            print(f"\r  ~ {utt.text[-90:]}", end="", flush=True)
            continue
        if utt.id in printed:
            # Çeviri, parça kesinleştikten sonra da gelebilir: aynı id ile güncellenir.
            print(f"    -> (geç çeviri) {utt.translation}")
            continue
        printed.add(utt.id)
        latency = time.monotonic() - (started[0] + utt.end_s)
        latencies.append(latency)
        print(f"\r[{utt.speaker_label or '?'}] {utt.text}")
        if utt.translation:
            print(f"    -> {utt.translation}")
        print(f"    ({utt.start_s:.1f}-{utt.end_s:.1f} sn, gecikme {latency * 1000:.0f} ms)")

    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p90 = latencies[int(len(latencies) * 0.9) - 1 if len(latencies) > 1 else 0]
        summary = f"p50 {p50 * 1000:.0f} ms, p90 {p90 * 1000:.0f} ms"
        print()
        print(f"{len(latencies)} final parça | gecikme {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
