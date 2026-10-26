"""Ses izi (speaker embedding) çıkarıcı.

Model: WeSpeaker ResNet34-LM (VoxCeleb), ONNX, 6.6M parametre, 256 boyutlu çıktı.
Lisans CC-BY-4.0 (ticari kullanım serbest, atıf gerekli — bkz. NOTICE). VPS CPU'sunda
birkaç saniyelik bir kesit onlarca milisaniyede işlenir; GPU gerekmez.

Öznitelik: Kaldi uyumlu fbank (80 mel, 25/10 ms, dither yok), dalga int16 ölçeğinde,
kesit başına ortalama çıkarma (CMN) — WeSpeaker'ın eğitim/çıkarım ön işlemesiyle aynı.
"""

from __future__ import annotations

import abc
import urllib.request
from pathlib import Path

import numpy as np

from katip.config import get_settings
from katip.logging import get_logger

log = get_logger(__name__)

MODEL_URL = (
    "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/"
    "resolve/main/voxceleb_resnet34_LM.onnx"
)
MIN_SECONDS = 1.0


class SpeakerEncoder(abc.ABC):
    name: str
    dim: int

    @abc.abstractmethod
    def embed(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray | None:
        """int16 mono PCM → L2-normalize ses izi. Kesit çok kısaysa None."""


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def ensure_model(path: Path) -> Path:
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    log.info("ses izi modeli indiriliyor", url=MODEL_URL, path=str(path))
    tmp = path.with_suffix(".part")
    urllib.request.urlretrieve(MODEL_URL, tmp)  # noqa: S310 — sabit HTTPS adresi
    tmp.replace(path)
    return path


class WeSpeakerEncoder(SpeakerEncoder):
    dim = 256

    def __init__(self, model_path: Path | None = None) -> None:
        import kaldi_native_fbank as knf
        import onnxruntime as ort

        settings = get_settings()
        self.name = settings.voice_model
        default = (
            Path(settings.voice_model_path)
            if settings.voice_model_path
            else Path(settings.data_dir) / "models" / "voxceleb_resnet34_LM.onnx"
        )
        path = ensure_model(model_path or default)
        opts = ort.SessionOptions()
        # Küçük VPS'te diğer işleri boğmasın.
        opts.intra_op_num_threads = 2
        self._session = ort.InferenceSession(
            str(path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._knf = knf

    def _fbank(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        opts = self._knf.FbankOptions()
        opts.frame_opts.samp_freq = sample_rate
        opts.frame_opts.frame_length_ms = 25
        opts.frame_opts.frame_shift_ms = 10
        opts.frame_opts.dither = 0.0
        opts.mel_opts.num_bins = 80
        fbank = self._knf.OnlineFbank(opts)
        # Kaldi konvansiyonu: dalga int16 ölçeğinde (±32768) verilir.
        fbank.accept_waveform(sample_rate, pcm.astype(np.float32).tolist())
        fbank.input_finished()
        feats = np.stack([fbank.get_frame(i) for i in range(fbank.num_frames_ready)])
        return feats - feats.mean(axis=0, keepdims=True)

    def embed(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray | None:
        if pcm.size < MIN_SECONDS * sample_rate:
            return None
        feats = self._fbank(pcm, sample_rate)[None, :, :].astype(np.float32)
        emb = self._session.run(["embs"], {"feats": feats})[0][0]
        return (emb / (np.linalg.norm(emb) + 1e-9)).astype(np.float32)
