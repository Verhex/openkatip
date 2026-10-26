"""Diarization etiketlerini ("1", "2") gerçek katılımcı isimlerine eşler.

Soniox kimin konuştuğunu *ayırt eder* ama adını bilmez; toplantı arayüzü ise kimin
konuştuğunu gösterir ama sesi ayırmaz. İkisini zaman üzerinden birleştiriyoruz:
bir konuşma parçası kesinleştiğinde, o zaman aralığında DOM'da "konuşuyor" görünen
kişilere o etiket için oy verilir. Birkaç parçadan sonra eşleme oturur.

Yalnızca taze ve tek konuşmacılı gözlemler öğrenilir. Bir isim için en az üç
ayrı cümle gerekir; çakışan kişi/etiket eşleşmeleri isimlendirilmez.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

_MIN_VOTES = 1.5
_MAX_OBSERVATION_AGE_S = 2.0
_MIN_SAMPLES = 3


class SpeakerMapper:
    def __init__(self, history: int = 2400) -> None:
        # (monotonic zaman, o an konuşan isimler)
        self._activity: deque[tuple[float, tuple[str, ...]]] = deque(maxlen=history)
        self._votes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._samples: dict[str, deque[tuple[str, float]]] = defaultdict(lambda: deque(maxlen=12))

    def observe(self, active_speakers: list[str], at: float | None = None) -> None:
        self._activity.append((at if at is not None else time.monotonic(), tuple(active_speakers)))

    def reset_labels(self) -> None:
        """A new STT stream has new labels; previous candidates must not compete."""
        self._votes.clear()
        self._samples.clear()

    def learn(self, label: str | None, t0: float, t1: float) -> None:
        """Kesinleşmiş bir parçadan (etiket + duvar saati aralığı) oy topla."""
        if label is None:
            return
        name = self.name_at(t0, t1)
        # One duration-weighted vote per utterance, independent of DOM update rate.
        # Ambiguous observations count against confidence, rather than disappearing.
        name = name or ""
        weight = min(max(t1 - t0, 0.0), 3.0) / 3.0
        samples = self._samples[label]
        if len(samples) == samples.maxlen:
            old_name, old_weight = samples[0]
            self._votes[label][old_name] -= old_weight
        samples.append((name, weight))
        self._votes[label][name] += weight

    def name_at(self, t0: float, t1: float) -> str | None:
        """Bu cümle sırasında "konuşuyor" görünen tek kişi (süre ağırlıklı).

        Bu yalnızca adaydır, doğrudan cümleye isim vermez. Tek bir kişi sürenin
        en az %75'inde görünmeli, diğer kişilerin her biri %15'in altında kalmalıdır.
        İki saniyeden eski gözlemler ve eşzamanlı konuşmalar isim kanıtı sayılmaz.
        """
        t1 = max(t1, t0 + 0.1)
        share: dict[str, float] = defaultdict(float)
        state: tuple[str, ...] = ()
        cursor = t0
        observed_at = -float("inf")
        for ts, names in self._activity:
            if ts <= t0:
                state = names
                observed_at = ts
                continue
            if ts >= t1:
                break
            self._add_share(share, state, min(ts, observed_at + _MAX_OBSERVATION_AGE_S) - cursor)
            cursor, state, observed_at = ts, names, ts
        self._add_share(share, state, min(t1, observed_at + _MAX_OBSERVATION_AGE_S) - cursor)
        total = t1 - t0
        if not share:
            return None
        name, seconds = max(share.items(), key=lambda kv: kv[1])
        others = [s for n, s in share.items() if n != name]
        if seconds / total >= 0.75 and all(s / total < 0.15 for s in others):
            return name
        return None

    @staticmethod
    def _add_share(share: dict[str, float], state: tuple[str, ...], seconds: float) -> None:
        if seconds <= 0 or len(state) != 1:
            return
        for name in state:
            share[name] += seconds / len(state)

    def name_for(self, label: str | None) -> str | None:
        if label is None:
            return None
        name = self._candidate(label)
        if name and not any(
            other != label and self._candidate(other) == name for other in self._votes
        ):
            return name
        return f"Konuşmacı {label}"

    def _candidate(self, label: str) -> str | None:
        votes = self._votes.get(label)
        if votes:
            name, score = max(votes.items(), key=lambda kv: kv[1])
            total = sum(votes.values())
            runner_up = max((v for n, v in votes.items() if n != name), default=0.0)
            samples = sum(n == name and w > 0 for n, w in self._samples[label])
            if (
                name
                and total > 0
                and samples >= _MIN_SAMPLES
                and score >= _MIN_VOTES
                and score / total >= 0.8
                and (score - runner_up) / total >= 0.6
            ):
                return name
        return None
