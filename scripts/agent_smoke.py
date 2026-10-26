"""Katip agent'ını gerçek Claude API'siyle dener (toplantısız, birkaç sentlik çağrı).

Araç şemalarının, düşünme/efor ayarlarının canlı API'de kabul edildiğini ve modelin istekleri
doğru araçlara çevirdiğini görmek için. Kullanım: python scripts/agent_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import time

from katip.agent.brain import TurnContext, run_turn

TRANSCRIPT = """Deniz Kaya: Q4 bütçesine bakalım, pazarlama için 1,2 milyon ayırdık.
Selin Arslan: Ajans seçimini cuma günü netleştirelim, üç teklif geldi.
Deniz Kaya: Lansmanı 15 Kasım'a çekelim."""

CASES = [
    "Katip, iki numaralı aksiyonun terminini 1 Ekim 2026 yap.",
    "Katip, şunu İngilizceye çevirip söyle: satışta termin tarihimiz 10 Ekim, ödeme Ekim sonu.",
    "Evet, bunu da aksiyon olarak kaydet.",
    "Katip, bundan sonra Türkçe söylediklerimi hep İngilizceye çevir.",
    "Katip, karar şu: lansman 15 Kasım'da yapılacak.",
    "Katip, Selin'e bir aksiyon aç, ajans tekliflerini cumaya kadar karşılaştırsın.",
    "Katip, şu ana kadar ne konuştuk?",
    "Katip, sesli cevap vermeye başla ve ekranın görüntüsünü al.",
    "Katip bugün çok sessiz, değil mi?",
    "Katip, bunu Ahmet'e mail at.",
]


async def main() -> int:
    ok = True
    for request in CASES:
        calls: list[tuple[str, dict]] = []

        async def executor(name: str, args: dict, calls=calls) -> str:
            calls.append((name, args))
            return "Tamam."

        t0 = time.monotonic()
        reply = await run_turn(
            request,
            speaker="Deniz Kaya",
            source="voice",
            context=TurnContext(
                transcript=TRANSCRIPT,
                participants=["Deniz Kaya <deniz@example.com>", "Selin Arslan"],
                notes=[
                    "#1 Aksiyon: Yanıt sürelerini analiz et (sorumlu: Deniz Kaya)",
                    "#2 Aksiyon: Rapor linkini Teams chat'ine gönder (sorumlu: Deniz Kaya)",
                ],
                state={"cevap modu": "yazılı", "kayıt": "kayıtta"},
            ),
            executor=executor,
            company="Example",
        )
        took = time.monotonic() - t0
        print(f"\n> {request}\n  araçlar: {calls}\n  cevap : {reply!r}  ({took:.1f} sn)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
