"""Ses izlerinin (biyometrik veri) şifrelenmesi: AES-256-GCM.

KVKK Kurulu'nun özel nitelikli veriler için yeterli önlemler kararı (2018/10) şifreleme ve
anahtarın veriden ayrı ortamda tutulmasını ister. Anahtar `KATIP_VOICEPRINT_KEY` ortam
değişkenindedir (Railway değişkeni), veri ise veritabanında (Railway volume). Anahtar yoksa
ses tanıma çalışmaz: biyometrik veri hiçbir durumda şifresiz yazılmaz.

Biçim: b"KV1" | nonce (12 bayt) | şifreli metin + etiket. Ek doğrulanmış veri (AAD) kişi no
ve model adıdır: bir kişinin şifreli izi başka bir kişinin satırına kopyalanırsa çözülmez.

Anahtar üretmek için:
    python -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
"""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from katip.config import get_settings

MAGIC = b"KV1"


class VoiceprintKeyError(RuntimeError):
    """Anahtar tanımlı değil ya da geçersiz."""


def _key() -> bytes:
    raw = get_settings().voiceprint_key.strip()
    if not raw:
        raise VoiceprintKeyError("KATIP_VOICEPRINT_KEY tanımlı değil")
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VoiceprintKeyError("KATIP_VOICEPRINT_KEY base64 değil") from exc
    if len(key) != 32:
        raise VoiceprintKeyError("KATIP_VOICEPRINT_KEY 32 bayt (256 bit) olmalı")
    return key


def available() -> bool:
    try:
        _key()
    except VoiceprintKeyError:
        return False
    return True


def _aad(person_id: int, model: str) -> bytes:
    return f"katip-voiceprint:{person_id}:{model}".encode()


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def encrypt(data: bytes, *, person_id: int, model: str) -> bytes:
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(_key()).encrypt(nonce, data, _aad(person_id, model))


def decrypt(blob: bytes, *, person_id: int, model: str) -> bytes:
    if not is_encrypted(blob):
        raise ValueError("şifreli ses izi değil")
    nonce, body = blob[3:15], blob[15:]
    return AESGCM(_key()).decrypt(nonce, body, _aad(person_id, model))
