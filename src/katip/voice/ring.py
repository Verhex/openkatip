"""STT'ye giden karışık sesin kısa geçmişi — konuşma parçalarını sesten kesmek için.

STT sağlayıcısı zamanları kendi akışının başına göre verir (start_ms). Tampon, STT'ye
*gönderilen* her frame'i sırayla tutar ve akış (yeniden) başladığında o anki örnek
sayısını taban olarak işaretler; böylece "akışın 12.4. saniyesi" tam olarak hangi
örneklere denk geliyor, kaymadan bulunur.
"""

from __future__ import annotations

import numpy as np


class AudioRing:
    def __init__(self, sample_rate: int, seconds: float = 180.0) -> None:
        self.sample_rate = sample_rate
        self._cap = int(sample_rate * seconds)
        self._buf = np.zeros(self._cap, dtype=np.int16)
        self._total = 0  # şimdiye kadar yazılan toplam örnek
        self._stream_base = 0

    @property
    def total_samples(self) -> int:
        return self._total

    def mark_stream_start(self) -> None:
        self._stream_base = self._total

    def append(self, frame: bytes) -> None:
        samples = np.frombuffer(frame, dtype=np.int16)
        original = samples.size
        if original == 0:
            return
        if original > self._cap:
            samples = samples[-self._cap :]
        n = samples.size
        # Zaman hizası için sayaç her zaman gelen örnek sayısı kadar ilerler.
        start = (self._total + original - n) % self._cap
        first = min(n, self._cap - start)
        self._buf[start : start + first] = samples[:first]
        if first < n:
            self._buf[: n - first] = samples[first:]
        self._total += original

    def slice_stream(self, start_s: float, end_s: float) -> np.ndarray | None:
        """Akış zamanına göre [start_s, end_s) aralığı; tampondan taşmışsa None."""
        lo = self._stream_base + int(start_s * self.sample_rate)
        hi = self._stream_base + int(end_s * self.sample_rate)
        hi = min(hi, self._total)
        if hi <= lo or lo < self._total - self._cap or lo < 0:
            return None
        idx = np.arange(lo, hi) % self._cap
        return self._buf[idx].copy()
