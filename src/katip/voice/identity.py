"""Toplantı içi ses kimliği: diarization etiketini kayıtlı kişilere bağlar, kayıt ve unutma.

Akış:
- Her kesinleşmiş parçanın sesi (AudioRing'den) ses izine çevrilir ve o parçanın
  diarization etiketine ("1", "2") biriktirilir. Etiketin ağırlıklı ortalaması, o
  toplantıdaki "ses kimliği"dir.
- Yeterli konuşma biriktiğinde bu ortalama, açık rızayla kaydedilmiş ses izleriyle
  karşılaştırılır. En iyi skor eşiği geçiyor VE ikinciden yeterince ayrışıyorsa eşleşme
  kabul edilir.
- Kayıt ("sesimi tanı"): rıza alındıktan sonra etiketin ortalaması kişinin ses izi olur.
  Yeterli konuşma yoksa kayıt bekler ve konuşma biriktikçe kendiliğinden tamamlanır.

Eşik değerleri VoxCeleb'e göre makul başlangıçlardır; toplantı sesi (Opus codec, farklı
mikrofonlar) skorları düşürebilir. İlk gerçek toplantılarla kalibre edilmeli
(bkz. `match_threshold`, `KATIP_VOICE_MATCH_THRESHOLD`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from katip.logging import get_logger
from katip.voice.encoder import SpeakerEncoder, cosine

log = get_logger(__name__)


@dataclass(frozen=True)
class KnownVoice:
    person_id: int
    name: str
    role: str
    embedding: np.ndarray


@dataclass(frozen=True)
class VoiceMatch:
    person_id: int
    name: str
    role: str
    score: float


@dataclass
class _LabelVoice:
    weighted_sum: np.ndarray | None = None
    seconds: float = 0.0
    segments: int = 0
    match: VoiceMatch | None = None
    greeted: bool = False
    parts: list = field(default_factory=list)  # (embedding, saniye)

    def add(self, emb: np.ndarray, seconds: float) -> None:
        w = emb * seconds
        self.weighted_sum = w if self.weighted_sum is None else self.weighted_sum + w
        self.seconds += seconds
        self.segments += 1
        self.parts.append((emb, seconds))

    def split(self) -> tuple[float, float]:
        """Parçaları iki kümeye ayırır: (kümeler arası benzerlik, küçük kümenin süre payı).

        Tek kişi: kümeler çok benzer ya da biri çok küçük (tek gürültülü parça). İki kişi aynı
        etikete düşmüşse (aynı cihazdan konuşan iki kişi): iki belirgin ve benzemeyen küme.
        """
        if len(self.parts) < 2:
            return 1.0, 0.0
        embs = [e / (np.linalg.norm(e) + 1e-9) for e, _ in self.parts]
        secs = np.array([s for _, s in self.parts], dtype=float)
        sims = np.array([[float(a @ b) for b in embs] for a in embs])
        i, j = np.unravel_index(np.argmin(sims), sims.shape)
        centers = [embs[i], embs[j]]
        assign = np.zeros(len(embs), dtype=int)
        for _ in range(8):
            assign = np.array([0 if e @ centers[0] >= e @ centers[1] else 1 for e in embs])
            for k in (0, 1):
                members = [embs[n] * secs[n] for n in range(len(embs)) if assign[n] == k]
                if members:
                    c = np.sum(members, axis=0)
                    centers[k] = c / (np.linalg.norm(c) + 1e-9)
        shares = [secs[assign == k].sum() / secs.sum() for k in (0, 1)]
        return float(centers[0] @ centers[1]), float(min(shares))

    @property
    def centroid(self) -> np.ndarray | None:
        if self.weighted_sum is None:
            return None
        return self.weighted_sum / (np.linalg.norm(self.weighted_sum) + 1e-9)


@dataclass
class VoiceIdentifier:
    encoder: SpeakerEncoder
    sample_rate: int
    known: list[KnownVoice] = field(default_factory=list)
    match_threshold: float = 0.55
    match_margin: float = 0.08
    min_segment_s: float = 1.5
    min_match_s: float = 4.0
    min_enroll_s: float = 8.0
    _labels: dict[str, _LabelVoice] = field(default_factory=dict)

    # ------------------------------------------------------------ öğrenme
    def observe(
        self,
        label: str | None,
        pcm: np.ndarray | None,
        *,
        candidates: set[int] | frozenset[int] = frozenset(),
        enrolling: bool = False,
    ) -> VoiceMatch | None:
        """Bir parçanın sesini etiketine ekler; yeni bir eşleşme oluştuysa döner.

        Rızaya bağlı işleme: ses izi (embedding) yalnızca iki durumda çıkarılır —
        (1) kişi sesli açık rıza verip kaydını tamamlıyorsa (`enrolling`), (2) konuşmacı ekrandaki
        adıyla, rızayla kaydolmuş belirli kişilere aday gösterilmişse (`candidates`): o zaman
        yalnızca o kişilerle 1:1 doğrulama yapılır. Diğer herkesin sesinden hiçbir iz çıkarılmaz
        ve daha önce çıkarılmış geçici izler de silinir.
        """
        if label is None or pcm is None:
            return None
        voice = self._labels.get(label)
        if voice is not None and voice.match is not None and not enrolling:
            return None  # zaten tanındı: yeni iz çıkarmaya gerek yok
        if not enrolling and not candidates:
            if voice is not None and voice.match is None:
                self._labels.pop(label, None)
            return None
        seconds = pcm.size / self.sample_rate
        if seconds < self.min_segment_s:
            return None
        emb = self.encoder.embed(pcm, self.sample_rate)
        if emb is None:
            return None
        voice = self._labels.setdefault(label, _LabelVoice())
        voice.add(emb, seconds)
        if voice.match is None and candidates and voice.seconds >= self.min_match_s:
            voice.match = self._best_match(voice.centroid, candidates)
            if voice.match:
                log.info(
                    "ses tanındı",
                    label=label,
                    person_id=voice.match.person_id,
                    score=round(voice.match.score, 3),
                )
                return voice.match
        return None

    def _best_match(
        self, centroid: np.ndarray | None, candidates: set[int] | frozenset[int]
    ) -> VoiceMatch | None:
        pool = [k for k in self.known if k.person_id in candidates]
        if centroid is None or not pool:
            return None
        scored = sorted(
            ((cosine(centroid, k.embedding), k) for k in pool),
            key=lambda sk: sk[0],
            reverse=True,
        )
        best_score, best = scored[0]
        second = scored[1][0] if len(scored) > 1 else -1.0
        if best_score < self.match_threshold or best_score - second < self.match_margin:
            # Eşiği canlı veriyle ayarlayabilmek için en yakın adayın skoru loglanır
            # (kişisel veri yok: yalnızca kişi no ve skor).
            log.info(
                "ses eşleşmedi",
                best_person_id=best.person_id,
                best=round(best_score, 3),
                second=round(second, 3),
                threshold=self.match_threshold,
                margin=self.match_margin,
            )
            return None
        return VoiceMatch(best.person_id, best.name, best.role, best_score)

    def identify_segment(
        self, pcm: np.ndarray | None, candidates: set[int] | frozenset[int]
    ) -> VoiceMatch | None:
        """Bir cümleyi yalnız aday kişilerle (toplantıda bulunan, rıza vermiş) karşılaştırır.

        Etikete kalıcı ad bağlanmaz: her cümle ayrı değerlendirilir; iki kişi aynı etikete
        düşse de (aynı cihazdan konuşanlar) her cümle kendi sahibine yazılır.
        """
        if pcm is None or not candidates:
            return None
        if pcm.size / self.sample_rate < self.min_segment_s:
            return None
        emb = self.encoder.embed(pcm, self.sample_rate)
        if emb is None:
            return None
        return self._best_match(emb / (np.linalg.norm(emb) + 1e-9), candidates)

    # ------------------------------------------------------------ sorgular
    def match_for(self, label: str | None) -> VoiceMatch | None:
        voice = self._labels.get(label) if label else None
        return voice.match if voice else None

    def speech_seconds(self, label: str | None) -> float:
        voice = self._labels.get(label) if label else None
        return voice.seconds if voice else 0.0

    def centroid(self, label: str | None) -> np.ndarray | None:
        voice = self._labels.get(label) if label else None
        return voice.centroid if voice else None

    def mark_greeted(self, label: str) -> bool:
        """İlk kez çağrıldığında True (selamla), sonra False."""
        voice = self._labels.get(label)
        if voice is None or voice.greeted:
            return False
        voice.greeted = True
        return True

    # ------------------------------------------------------ kayıt / unutma
    def ready_to_enroll(self, label: str) -> bool:
        return self.speech_seconds(label) >= self.min_enroll_s

    def split(self, label: str) -> tuple[float, float]:
        voice = self._labels.get(label)
        return voice.split() if voice else (1.0, 0.0)

    def reset(self, label: str) -> None:
        """Etiketin geçici ses izlerini at (ör. karışık ses yüzünden kayıt baştan alınacak)."""
        self._labels.pop(label, None)

    def register(self, label: str, known: KnownVoice) -> None:
        """Yeni kaydedilen kişiyi bu toplantıda hemen tanı."""
        self.known = [k for k in self.known if k.person_id != known.person_id] + [known]
        voice = self._labels.setdefault(label, _LabelVoice())
        voice.match = VoiceMatch(known.person_id, known.name, known.role, 1.0)
        voice.greeted = True

    def forget(self, person_id: int) -> None:
        self.known = [k for k in self.known if k.person_id != person_id]
        for voice in self._labels.values():
            if voice.match and voice.match.person_id == person_id:
                voice.match = None
