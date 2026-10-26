"""Toplantı içeriği için zarflı AES-256-GCM şifreleme.

Blob: KM | sürüm (1 bayt) | nonce (12 bayt) | şifreli veri ve GCM etiketi.
AAD kurulum, toplantı ve alanı (satır kimliğiyle) veya dosya adını bağlar.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Session, attributes, object_session

from katip.config import get_settings
from katip.store.models import (
    EncryptionInstallation,
    Meeting,
    MeetingAsset,
    MeetingDataKey,
    MeetingIdentity,
    MeetingMailDraft,
    MeetingNote,
    MeetingParticipant,
    MeetingSegment,
    MeetingTombstone,
    PreparationDocument,
    TeamsConversation,
    WorkerJob,
)

MAGIC_V1 = b"KM\x01"
MAGIC = b"KM\x02"
FIELDS = {
    Meeting: ("title", "url", "report"),
    MeetingSegment: ("text", "translation", "speaker_name", "speaker_email"),
    MeetingNote: ("text", "author"),
    MeetingParticipant: ("name", "email", "platform_id"),
    MeetingIdentity: ("name", "email", "platform_id"),
    PreparationDocument: ("name", "pages"),
    MeetingAsset: ("requested_by", "caption"),
    WorkerJob: ("spec",),
    MeetingMailDraft: ("payload",),
    TeamsConversation: ("reference",),
}


class DataKeyError(RuntimeError):
    """Toplantı anahtarı eksik veya geçersiz."""


def _decode(raw: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DataKeyError("Veri anahtarı geçerli base64 olmalı") from exc
    if len(key) != 32:
        raise DataKeyError("Veri anahtarı 32 bayt (256 bit) olmalı")
    return key


def keys() -> tuple[bytes | None, bytes | None]:
    settings = get_settings()
    raw = settings.data_key
    if settings.data_key_file:
        try:
            raw = Path(settings.data_key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise DataKeyError("Veri anahtarı dosyası okunamadı") from exc
        if not raw:
            raise DataKeyError("Veri anahtarı dosyası boş")
    current = _decode(raw) if raw.strip() else None
    previous = _decode(settings.data_key_previous) if settings.data_key_previous.strip() else None
    if current is None and (settings.require_encryption or previous is not None):
        raise DataKeyError("KATIP_DATA_KEY veya KATIP_DATA_KEY_FILE zorunlu")
    return current, previous


def available() -> bool:
    return keys()[0] is not None


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith((MAGIC_V1, MAGIC))


def seal(key: bytes, data: bytes, aad: bytes, *, magic: bytes = MAGIC_V1) -> bytes:
    nonce = os.urandom(12)
    return magic + nonce + AESGCM(key).encrypt(nonce, data, aad)


def open_blob(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if not is_encrypted(blob):
        raise DataKeyError("Desteklenmeyen şifreli veri sürümü")
    return AESGCM(key).decrypt(blob[3:15], blob[15:], aad)


@dataclass(frozen=True)
class MeetingCipher:
    key: bytes = dataclass_field(repr=False)
    installation_id: str
    meeting_id: str
    strict: bool = False

    def subkey(self, label: str) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=label.encode()).derive(
            self.key
        )

    def aad(self, field: str) -> bytes:
        return json.dumps(
            ["katip-content", 1, self.installation_id, self.meeting_id, field],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    def encrypt(self, data: bytes, field: str) -> bytes:
        return seal(self.subkey("katip:content:v2"), data, self.aad(field), magic=MAGIC)

    def decrypt(self, blob: bytes, field: str) -> bytes:
        key = self.subkey("katip:content:v2") if blob.startswith(MAGIC) else self.key
        return open_blob(key, blob, self.aad(field))

    def lookup(self, model, field: str, value: str, *, version: int = 2) -> str:
        """Ad/e-posta benzersizliği için toplantıya bağlı, anahtarlı arama özeti."""
        aad = self.aad(f"lookup:{model.__tablename__}:{field}")
        key = self.key if version == 1 else self.subkey("katip:lookup:v2")
        prefix = "km:" if version == 1 else "km2:"
        return prefix + hmac.new(key, aad + value.encode(), hashlib.sha256).hexdigest()

    def document_digest(self, data: bytes) -> str:
        return self.digest_from_sha256(hashlib.sha256(data).hexdigest())

    def digest_from_sha256(self, digest: str) -> str:
        fingerprint = document_fingerprint(self.installation_id, self.meeting_id, digest)
        return self.keyed_document_fingerprint(fingerprint)

    def keyed_document_fingerprint(self, fingerprint: str) -> str:
        return hmac.new(
            self.subkey("katip:lookup:v2"),
            b"document:" + bytes.fromhex(fingerprint),
            hashlib.sha256,
        ).hexdigest()

    def read_file(self, path: Path) -> bytes:
        data = path.read_bytes()
        if self.strict and not is_encrypted(data):
            raise DataKeyError("Şifrelenmesi gereken dosyada düz metin reddedildi")
        return self.decrypt(data, "file:" + path.name) if is_encrypted(data) else data

    def write_file(self, path: Path, data: bytes) -> None:
        with file_lock(self.meeting_id):
            self._write_file(path, data)

    def _write_file(self, path: Path, data: bytes) -> None:
        blob = self.encrypt(data, "file:" + path.name)
        atomic_write(path, blob, lambda saved: self.decrypt(saved, "file:" + path.name) == data)

    def migrate_file(self, path: Path) -> bool:
        with file_lock(self.meeting_id):
            if not path.is_file():
                return False
            raw = path.read_bytes()
            if is_encrypted(raw):
                self.decrypt(raw, "file:" + path.name)
                return False
            if self.strict:
                raise DataKeyError("Göç tamamlandıktan sonra düz dosya reddedildi")
            self._write_file(path, raw)
            return True


def lock_name(meeting_id: str) -> str:
    return hashlib.sha256(meeting_id.encode()).hexdigest() + ".lock"


def _deleted_path(meeting_id: str) -> Path:
    # Kalıcı silme kaydı kilit dosyasından bağımsızdır; eski cipher da reddedilir.
    return Path(get_settings().data_dir) / ".deleted" / lock_name(meeting_id)


def _try_lock(stream) -> bool:
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _open_lock(path: Path):
    # Windows bayt kilidi gerçek dosya konumunu kullanır; tamponlu seek yeterli değildir.
    stream = path.open("a+b", buffering=0)
    if path.stat().st_size == 0:
        stream.write(b"0")
        stream.flush()
    return stream


@contextmanager
def _lock_directory():
    """Açma ve kaldırmayı sırala; bekleyen süreç eski inode üzerinde kilit alamaz."""
    data = Path(get_settings().data_dir)
    (data / ".locks").mkdir(parents=True, exist_ok=True)
    with _open_lock(data / ".locks.guard") as guard:
        while not _try_lock(guard):
            time.sleep(0.01)
        try:
            yield data / ".locks"
        finally:
            _unlock(guard)


@contextmanager
def file_lock(meeting_id: str, *, deleting: bool = False):
    """Süreçler arası kilit; beklerken dizin kilidi veya açık dosya tutulmaz."""
    while True:
        with _lock_directory() as folder:
            if not deleting and _deleted_path(meeting_id).exists():
                raise DataKeyError("Silinen toplantıya dosya yazılamaz")
            stream = _open_lock(folder / lock_name(meeting_id))
            if _try_lock(stream):
                break
            stream.close()
        time.sleep(0.05)
    try:
        stream.seek(0)
        if stream.read(1) == b"1" and not deleting:
            raise DataKeyError("Silinen toplantıya dosya yazılamaz")
        yield stream
    finally:
        _unlock(stream)
        stream.close()


def mark_deleted(meeting_id: str, stream) -> None:
    atomic_write(_deleted_path(meeting_id), b"1")
    stream.seek(0)
    stream.truncate(0)
    stream.write(b"1")
    stream.flush()
    os.fsync(stream.fileno())


def _cleanup_lock_path(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    with path.open("r+b", buffering=0) as stream:
        if not _try_lock(stream):
            return False
        try:
            # Önceki sürümün kalıcı silme işaretini kilitten ayır.
            stream.seek(0)
            if stream.read(1) == b"1":
                atomic_write(Path(get_settings().data_dir) / ".deleted" / path.name, b"1")
        finally:
            _unlock(stream)
    # Çağıran dizin kilidini tutar: arada yeni açan veya bekleyen olamaz.
    path.unlink(missing_ok=True)
    return True


def cleanup_lock(meeting_id: str) -> bool:
    with _lock_directory() as folder:
        return _deleted_path(meeting_id).exists() and _cleanup_lock_path(
            folder / lock_name(meeting_id)
        )


def cleanup_orphan_locks(live_ids: set[str]) -> int:
    live = {lock_name(mid) for mid in live_ids}
    with _lock_directory() as folder:
        return sum(
            _cleanup_lock_path(path) for path in folder.glob("*.lock") if path.name not in live
        )


async def repair_orphan_marker(meeting_id: str) -> None:
    """Eski sürümde başarısız commit öncesinde yazılmış işareti DB ile doğrula."""
    import asyncio

    from katip.store.db import session_scope

    guard = file_lock(meeting_id, deleting=True)
    acquiring = asyncio.create_task(asyncio.to_thread(guard.__enter__))
    try:
        stream = await asyncio.shield(acquiring)
    except asyncio.CancelledError:
        await acquiring
        await asyncio.to_thread(guard.__exit__, None, None, None)
        raise
    try:
        async with session_scope() as session:
            live = await session.scalar(select(Meeting.id).where(Meeting.id == meeting_id))
            dead = await session.get(MeetingTombstone, meeting_id)
        if live and not dead:
            stream.seek(0)
            stream.truncate(0)
            stream.write(b"0")
            stream.flush()
            os.fsync(stream.fileno())
            _deleted_path(meeting_id).unlink(missing_ok=True)
    finally:
        await asyncio.to_thread(guard.__exit__, None, None, None)


def assert_live(connection, meeting_id: str) -> None:
    if connection.scalar(
        select(MeetingTombstone.meeting_id).where(MeetingTombstone.meeting_id == meeting_id)
    ):
        raise DataKeyError("Silinen toplantı kimliği yeniden kullanılamaz")


def require_write_key(connection) -> None:
    if not available() and (
        connection.scalar(select(EncryptionInstallation.encryption_started))
        or connection.scalar(select(MeetingDataKey.meeting_id).limit(1))
    ):
        raise DataKeyError("Şifreleme başlamış kurulumda yeni içerik için veri anahtarı gerekli")


def strict_reads(connection) -> bool:
    return bool(
        get_settings().require_encryption
        and connection.scalar(select(EncryptionInstallation.migration_completed))
    )


def document_fingerprint(installation: str, meeting_id: str, digest: str) -> str:
    """Anahtarsız geliştirme modunda bile küresel, tuzsuz belge özeti tutulmaz.

    Bu tuz gizli değildir; şifreli mod ayrıca DEK arama alt anahtarıyla HMAC uygular.
    """
    salt = hashlib.sha256(f"katip:document:{installation}:{meeting_id}".encode()).digest()
    return hmac.new(salt, bytes.fromhex(digest), hashlib.sha256).hexdigest()


def atomic_write(path: Path, data: bytes, verify=None) -> None:
    """Doğrulanan geçici kopyayı atomik yerleştirir; hata eski dosyayı korur."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".katip-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if verify is not None and not verify(Path(temporary).read_bytes()):
            raise DataKeyError("Şifreli dosya doğrulanamadı")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _insert(connection, table, values, primary: str) -> None:
    if connection.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    connection.execute(
        insert(table).values(**values).on_conflict_do_nothing(index_elements=[primary])
    )


def cipher_for(connection, meeting_id: str, *, create: bool = False) -> MeetingCipher:
    assert_live(connection, meeting_id)
    current, previous = keys()
    if current is None:
        raise DataKeyError("Şifreli toplantıyı okumak için veri anahtarı gerekli")
    installation = connection.scalar(
        select(EncryptionInstallation.installation_id).where(EncryptionInstallation.id == 1)
    )
    if installation is None:
        raise DataKeyError("Şifreleme kurulum kimliği bulunamadı")
    table = MeetingDataKey.__table__
    row = connection.execute(select(table).where(table.c.meeting_id == meeting_id)).first()
    aad = json.dumps(["katip-dek", 1, installation, meeting_id], separators=(",", ":")).encode()
    if row is None and create:
        dek = os.urandom(32)
        _insert(
            connection,
            table,
            {
                "meeting_id": meeting_id,
                "key_id": key_id(current),
                "version": 1,
                "wrapped_key": seal(current, dek, aad),
            },
            "meeting_id",
        )
        row = connection.execute(select(table).where(table.c.meeting_id == meeting_id)).first()
        connection.execute(update(EncryptionInstallation).values(encryption_started=True))
    if row is None:
        raise DataKeyError("Toplantı veri anahtarı bulunamadı; toplantı silinmiş olabilir")
    if row.version != 1:
        raise DataKeyError("Desteklenmeyen toplantı anahtarı sürümü")
    kek = next((k for k in (current, previous) if k is not None and key_id(k) == row.key_id), None)
    if kek is None:
        raise DataKeyError("Toplantı için uygun veri anahtarı bulunamadı")
    return MeetingCipher(
        open_blob(kek, row.wrapped_key, aad), installation, meeting_id, strict_reads(connection)
    )


async def meeting_cipher(meeting_id: str, *, create: bool = False) -> MeetingCipher | None:
    from katip.store.db import session_scope
    from katip.store.repo import _ensure_meeting

    async with session_scope(write=create) as session:
        await session.run_sync(lambda s: assert_live(s.connection(), meeting_id))
        await session.run_sync(lambda s: require_write_key(s.connection()))
        if create:
            await _ensure_meeting(session, meeting_id)
        if not available():
            return None
        return await session.run_sync(
            lambda s: cipher_for(s.connection(), meeting_id, create=create)
        )


async def read_file(meeting_id: str, path: str | Path) -> bytes:
    import asyncio

    path = Path(path)
    data = await asyncio.to_thread(path.read_bytes)
    if not is_encrypted(data):
        from katip.store.db import session_scope

        async with session_scope() as session:
            if await session.run_sync(lambda s: strict_reads(s.connection())):
                raise DataKeyError("Şifrelenmesi gereken dosyada düz metin reddedildi")
        return data
    cipher = await meeting_cipher(meeting_id)
    if cipher is None:
        raise DataKeyError("Şifreli dosyayı okumak için veri anahtarı gerekli")
    return cipher.decrypt(data, "file:" + path.name)


async def write_file(meeting_id: str, path: Path, data: bytes) -> None:
    import asyncio

    cipher = await meeting_cipher(meeting_id, create=True)
    if cipher:
        await asyncio.to_thread(cipher.write_file, path, data)
    else:

        def write_plain():
            with file_lock(meeting_id):
                atomic_write(path, data)

        await asyncio.to_thread(write_plain)


async def lookup_value(session, model, meeting_id: str, field: str, value: str) -> str:
    if not available():
        return value
    cipher = await session.run_sync(lambda s: _session_cipher(s, meeting_id, create=True))
    return cipher.lookup(model, field, value)


async def lookup_values(session, model, meeting_id: str, field: str, value: str) -> list[str]:
    if not available():
        return [value]
    cipher = await session.run_sync(lambda s: _session_cipher(s, meeting_id, create=True))
    return [
        value,
        cipher.lookup(model, field, value),
        cipher.lookup(model, field, value, version=1),
    ]


def _session_cipher(session, meeting_id: str, *, create: bool = False) -> MeetingCipher:
    current, previous = keys()
    if current is None:
        raise DataKeyError("Şifreli toplantıyı okumak için veri anahtarı gerekli")
    token = (meeting_id, key_id(current), key_id(previous) if previous else None)
    cache = session.info.setdefault("meeting_ciphers", {})
    if token not in cache:
        cache[token] = cipher_for(session.connection(), meeting_id, create=create)
    return cache[token]


@event.listens_for(Session, "after_transaction_end")
def _clear_ciphers(session, transaction) -> None:
    if transaction.parent is None:
        session.info.pop("meeting_ciphers", None)


def _meeting_id(row) -> str:
    return row.id if isinstance(row, Meeting) else row.meeting_id


def _identity(row):
    return row.meeting_id if isinstance(row, WorkerJob) else row.id


def _placeholder(row, field, value, cipher=None):
    if value is None:
        return None
    if field == "report":
        return {}
    if field == "pages":
        return []
    if field in {"spec", "payload", "reference"}:
        return {}
    if isinstance(row, (MeetingSegment, MeetingParticipant, MeetingIdentity)) and field in {
        "name",
        "email",
        "speaker_name",
        "speaker_email",
    }:
        return cipher.lookup(type(row), field, value)
    return ""


def _store(mapper, connection, row) -> None:
    assert_live(connection, _meeting_id(row))
    require_write_key(connection)
    if not available():
        if row.encrypted_content is not None:
            raise DataKeyError("Şifreli toplantıyı değiştirmek için veri anahtarı gerekli")
        if isinstance(row, PreparationDocument) and row.digest_version in {None, 1}:
            installation = connection.scalar(select(EncryptionInstallation.installation_id))
            row.sha256 = document_fingerprint(installation, row.meeting_id, row.sha256)
            row.digest_version = 0
        return
    session = object_session(row)
    cipher = _session_cipher(session, _meeting_id(row), create=True)
    if isinstance(row, PreparationDocument) and row.digest_version != 2:
        row.sha256 = (
            cipher.keyed_document_fingerprint(row.sha256)
            if row.digest_version == 0
            else cipher.digest_from_sha256(row.sha256)
        )
        row.digest_version = 2
    fields = FIELDS[type(row)]
    changed = [f for f in fields if inspect(row).attrs[f].history.has_changes()]
    if inspect(row).persistent and not changed:
        return
    values = {f: getattr(row, f) for f in fields}
    if inspect(row).persistent:
        # Güncel blob kilit altında birleştirilir; eski ORM kopyası diğer alanları ezmez.
        primary = row.__table__.c.meeting_id if isinstance(row, WorkerJob) else row.__table__.c.id
        stored = (
            connection.execute(
                select(row.__table__).where(primary == _identity(row)).with_for_update()
            )
            .mappings()
            .first()
        )
        if stored is None:
            raise DataKeyError("Güncellenecek şifreli satır bulunamadı")
        latest = stored["encrypted_content"]
        changes = {f: values[f] for f in changed}
        values = changes if latest is not None else {**{f: stored[f] for f in fields}, **changes}
    else:
        latest = None
    session.info.setdefault("content_restore", []).append((row, values))
    if _identity(row) is not None:
        row.encrypted_content = {**(latest or {}), **_encrypt_values(row, values, cipher)}
    for field, value in values.items():
        setattr(row, field, _placeholder(row, field, value, cipher))


def _encrypt_values(row, values, cipher):
    payload = {}
    for field, value in values.items():
        aad = f"{row.__tablename__}:{_identity(row)}:{field}"
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        blob = cipher.encrypt(raw, aad)
        if cipher.decrypt(blob, aad) != raw:
            raise DataKeyError("Şifreli alan doğrulanamadı")
        payload[field] = base64.b64encode(blob).decode()
    return payload


def _after_insert(mapper, connection, row) -> None:
    if not available() or row.encrypted_content is not None:
        return
    session = object_session(row)
    values = next(values for obj, values in session.info["content_restore"] if obj is row)
    cipher = _session_cipher(session, _meeting_id(row))
    payload = _encrypt_values(row, values, cipher)
    connection.execute(
        row.__table__.update().where(row.__table__.c.id == row.id).values(encrypted_content=payload)
    )
    attributes.set_committed_value(row, "encrypted_content", payload)


@event.listens_for(Session, "after_flush_postexec")
def _restore(session, context) -> None:
    for row, values in session.info.pop("content_restore", []):
        for field, value in values.items():
            attributes.set_committed_value(row, field, value)


def _load(row, context) -> None:
    if row.encrypted_content is None:
        if strict_reads(object_session(row).connection()):
            raise DataKeyError("Şifrelenmesi gereken satırda düz metin reddedildi")
        return
    cipher = _session_cipher(object_session(row), _meeting_id(row))
    for field, value in decrypt_values(row, row.encrypted_content, cipher).items():
        attributes.set_committed_value(row, field, value)


def decrypt_values(row, payload: dict, cipher: MeetingCipher) -> dict:
    if not isinstance(payload, dict) or set(payload) != set(FIELDS[type(row)]):
        raise DataKeyError("Şifreli alan kümesi geçersiz")
    values = {}
    for field in FIELDS[type(row)]:
        blob = base64.b64decode(payload[field], validate=True)
        values[field] = json.loads(
            cipher.decrypt(blob, f"{row.__tablename__}:{_identity(row)}:{field}")
        )
    return values


def _refresh(row, context, attrs) -> None:
    _load(row, context)


for _model in FIELDS:
    event.listen(_model, "before_insert", _store)
    event.listen(_model, "before_update", _store)
    event.listen(_model, "after_insert", _after_insert)
    event.listen(_model, "load", _load)
    event.listen(_model, "refresh", _refresh)
