"""Ses tamponu hizalaması, kimlik eşleştirme ve gerçek WeSpeaker modeli."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from katip.voice.encoder import SpeakerEncoder, cosine
from katip.voice.identity import KnownVoice, VoiceIdentifier
from katip.voice.ring import AudioRing

SR = 16000
MODEL = Path(__file__).resolve().parents[1] / "data" / "models" / "voxceleb_resnet34_LM.onnx"


def frame(value: int, n: int) -> bytes:
    return np.full(n, value, dtype=np.int16).tobytes()


# ------------------------------------------------------------------- tampon


def test_ring_slices_by_stream_time_after_restart() -> None:
    ring = AudioRing(SR, seconds=10)
    ring.append(frame(1, SR))  # 1 sn: önceki akış
    ring.mark_stream_start()  # yeni STT akışı burada başladı
    ring.append(frame(2, SR // 2))
    ring.append(frame(3, SR // 2))
    part = ring.slice_stream(0.5, 1.0)
    assert part is not None and part.size == SR // 2 and set(part.tolist()) == {3}
    first = ring.slice_stream(0.0, 0.5)
    assert set(first.tolist()) == {2}


def test_ring_wraps_and_rejects_overwritten_audio() -> None:
    ring = AudioRing(SR, seconds=1)
    ring.mark_stream_start()
    for v in range(1, 4):  # 3 sn yaz, tampon 1 sn
        ring.append(frame(v, SR))
    assert ring.slice_stream(0.0, 0.5) is None  # üzerine yazıldı
    last = ring.slice_stream(2.0, 3.0)
    assert last is not None and set(last.tolist()) == {3}
    assert ring.total_samples == 3 * SR


def test_ring_oversized_frame_keeps_time_alignment() -> None:
    ring = AudioRing(SR, seconds=1)
    ring.mark_stream_start()
    ring.append(frame(7, 2 * SR))  # tampondan büyük tek frame
    assert ring.total_samples == 2 * SR
    tail = ring.slice_stream(1.5, 2.0)
    assert tail is not None and set(tail.tolist()) == {7}


# ------------------------------------------------------------ kimlik eşleme


class FakeEncoder(SpeakerEncoder):
    """Sesin genliğini 'kimlik' sayar: aynı genlik = aynı kişi."""

    name = "fake"
    dim = 8

    def embed(self, pcm, sample_rate):
        if pcm.size < sample_rate:
            return None
        v = np.zeros(self.dim, dtype=np.float32)
        v[int(abs(pcm[0])) % self.dim] = 1.0
        v += 0.05
        return v / np.linalg.norm(v)


def voice_of(k: int) -> np.ndarray:
    return FakeEncoder().embed(np.full(SR, k, np.int16), SR)


def make_identifier(known=()) -> VoiceIdentifier:
    return VoiceIdentifier(FakeEncoder(), SR, known=list(known))


def test_identifies_enrolled_speaker_after_enough_speech() -> None:
    ident = make_identifier(
        [
            KnownVoice(1, "Ayşe", "member", voice_of(3)),
            KnownVoice(2, "Mehmet", "admin", voice_of(5)),
        ]
    )
    speech = np.full(2 * SR, 3, np.int16)
    both = {1, 2}
    assert ident.observe("1", speech, candidates=both) is None  # 2 sn < min_match_s (4 sn)
    match = ident.observe("1", speech, candidates=both)
    assert match is not None and match.name == "Ayşe" and match.person_id == 1
    assert ident.match_for("1") == match
    assert ident.observe("1", speech, candidates=both) is None  # yeni eşleşme değil


def test_unknown_voice_is_not_forced_onto_someone() -> None:
    ident = make_identifier([KnownVoice(1, "Ayşe", "member", voice_of(3))])
    for _ in range(3):
        assert ident.observe("2", np.full(2 * SR, 6, np.int16), candidates={1}) is None
    assert ident.match_for("2") is None


def test_short_segments_are_ignored() -> None:
    ident = make_identifier()
    ident.observe("1", np.full(SR, 3, np.int16), enrolling=True)  # 1 sn < 1.5 sn
    assert ident.speech_seconds("1") == 0.0


def test_enroll_readiness_register_and_forget() -> None:
    ident = make_identifier()
    for _ in range(3):
        ident.observe("1", np.full(3 * SR, 4, np.int16), enrolling=True)
    assert ident.ready_to_enroll("1")  # 9 sn ≥ 8 sn
    ident.register("1", KnownVoice(9, "Zeynep", "member", ident.centroid("1")))
    assert ident.match_for("1").name == "Zeynep"
    ident.forget(9)
    assert ident.match_for("1") is None and ident.known == []


# -------------------------------------------------------- gerçek WeSpeaker


@pytest.mark.skipif(not MODEL.exists(), reason="WeSpeaker modeli indirilmemis")
def test_wespeaker_embeddings_are_normalized_and_discriminative() -> None:
    from katip.voice.encoder import WeSpeakerEncoder

    enc = WeSpeakerEncoder(model_path=MODEL)
    rng = np.random.default_rng(0)
    t = np.arange(3 * SR) / SR

    def voice(f0: float, seed: int) -> np.ndarray:
        # Harmonik zengin sentetik "ses" (farklı temel frekans ≈ farklı konuşmacı).
        r = np.random.default_rng(seed)
        sig = sum(np.sin(2 * np.pi * f0 * h * t) / h for h in range(1, 12))
        sig = sig * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t)) + 0.05 * r.standard_normal(t.size)
        return (sig / np.abs(sig).max() * 12000).astype(np.int16)

    a1, a2, b = (
        enc.embed(voice(110, 1), SR),
        enc.embed(voice(110, 2), SR),
        enc.embed(voice(230, 3), SR),
    )
    assert a1.shape == (256,) and abs(np.linalg.norm(a1) - 1) < 1e-4
    assert cosine(a1, a2) > cosine(a1, b)  # aynı "ses" farklı gürültüyle daha yakın
    assert enc.embed(rng.integers(-100, 100, SR // 2).astype(np.int16), SR) is None  # < 1 sn


def test_no_voiceprint_is_extracted_without_consent_or_candidate() -> None:
    """Rıza vermemiş, kimseye aday gösterilmemiş konuşmacının sesinden iz çıkarılmaz."""
    calls = []

    class CountingEncoder(FakeEncoder):
        def embed(self, pcm, sr):
            calls.append(1)
            return super().embed(pcm, sr)

    ident = VoiceIdentifier(
        CountingEncoder(), SR, known=[KnownVoice(1, "Ayşe", "member", voice_of(3))]
    )
    for _ in range(4):
        assert ident.observe("7", np.full(3 * SR, 3, np.int16)) is None
    assert calls == [] and ident.speech_seconds("7") == 0.0
    # Aday gösterilince yalnızca o kişiyle doğrulanır.
    ident.observe("7", np.full(3 * SR, 3, np.int16), candidates={1})
    assert ident.observe("7", np.full(3 * SR, 3, np.int16), candidates={1}).person_id == 1
    # Aday gösterilen kişi dışındakilerle asla eşleşmez.
    ident2 = VoiceIdentifier(
        FakeEncoder(), SR, known=[KnownVoice(1, "Ayşe", "member", voice_of(3))]
    )
    for _ in range(3):
        assert ident2.observe("8", np.full(3 * SR, 3, np.int16), candidates={99}) is None


def test_voiceprints_are_encrypted_at_rest(tmp_path, monkeypatch) -> None:
    import base64
    import secrets

    from katip.config import get_settings
    from katip.voice import crypto

    monkeypatch.setenv("KATIP_VOICEPRINT_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    get_settings.cache_clear()
    vec = voice_of(3).astype(np.float32).tobytes()
    blob = crypto.encrypt(vec, person_id=5, model="m")
    assert blob.startswith(b"KV1") and vec not in blob
    assert crypto.decrypt(blob, person_id=5, model="m") == vec
    import pytest
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        crypto.decrypt(blob, person_id=6, model="m")  # başka kişinin satırına taşınamaz
    monkeypatch.setenv("KATIP_VOICEPRINT_KEY", "")
    get_settings.cache_clear()
    assert not crypto.available()
