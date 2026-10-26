"""Örnek sayacıyla sınırlı ses geçmişi ve sağlayıcı geçişindeki örtüşme."""

from collections import deque
from dataclasses import replace

from katip.pipeline.stt.base import Utterance


class ReplayBuffer:
    def __init__(self, sample_rate: int, seconds: int = 30):
        self.rate = sample_rate
        self.capacity = sample_rate * seconds
        self.total = 0
        self.frames: deque[tuple[int, bytes]] = deque()

    @property
    def oldest(self) -> int:
        return max(0, self.total - self.capacity)

    def append(self, frame: bytes) -> None:
        self.frames.append((self.total, frame))
        self.total += len(frame) // 2
        while self.frames and self.frames[0][0] + len(self.frames[0][1]) // 2 <= self.oldest:
            self.frames.popleft()
        if self.frames and self.frames[0][0] < self.oldest:
            start, data = self.frames.popleft()
            self.frames.appendleft((self.oldest, data[(self.oldest - start) * 2 :]))

    def replay(self, finalized: int, overlap_s: float = 0.5) -> tuple[int, list[bytes], bool]:
        wanted = max(0, finalized - int(overlap_s * self.rate))
        start = max(wanted, self.oldest)
        data = b"".join(
            frame[max(0, start - offset) * 2 :]
            for offset, frame in self.frames
            if offset + len(frame) // 2 > start
        )
        step = self.rate * 2 // 5
        return (
            start,
            [data[i : i + step] for i in range(0, len(data), step)],
            finalized < self.oldest,
        )


def deduplicate(utt: Utterance, boundary: float, previous: str) -> Utterance | None:
    if utt.end_s <= boundary:
        return None
    if utt.start_s >= boundary:
        return utt
    old, new = previous.split(), utt.text.split()
    for n in range(min(len(old), len(new)), 0, -1):
        if [w.casefold().strip(".,!?;:") for w in old[-n:]] == [
            w.casefold().strip(".,!?;:") for w in new[:n]
        ]:
            new = new[n:]
            break
    if not new:
        return None
    return replace(utt, text=" ".join(new), start_s=boundary)
