"""Veritabanı bağlantısı: SQLite (üretim) ve Postgres (asyncpg)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, inspect, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from katip.async_utils import LoopLocal, raise_if_cancelling
from katip.config import get_settings
from katip.logging import get_logger
from katip.store import crypto
from katip.store.models import Base

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_databases: LoopLocal[tuple[AsyncEngine, async_sessionmaker[AsyncSession]] | None] = LoopLocal(
    lambda: None
)
_pending_url: str | None = None


def _database() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    global _pending_url
    state = _databases.get()
    if state is None:
        url, _pending_url = _pending_url, None
        configure(url)
        state = _databases.get()
    assert state is not None
    return state


def _enable_sqlite_fk(engine: AsyncEngine) -> None:
    # SQLite yabancı anahtar zincirlemelerini (CASCADE / SET NULL) varsayılan olarak uygulamaz.
    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # Silinen satırların (ses izi, "beni unut") içeriği dosyada kalmasın: sıfırla üzerine yaz.
        cursor.execute("PRAGMA secure_delete=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def warn_if_not_persistent(db_path: Path) -> bool:
    """Railway'de SQLite dosyası kalıcı birimin (volume) dışındaysa her deploy'da silinir.

    30 Eylül'de yaşandı: `sqlite+aiosqlite:///data/katip.db` (üç eğik çizgi = göreli yol)
    veritabanını /app/data'ya yazdı; toplantılar ve ses izleri her deploy'da kayboldu.
    """
    import os

    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if not mount:
        return True
    resolved, volume = db_path.resolve(), Path(mount).resolve()
    if volume == resolved or volume in resolved.parents:
        return True
    log.error(
        "VERİTABANI KALICI DEĞİL: her deploy'da silinecek",
        db_path=str(resolved),
        volume=str(volume),
        fix="KATIP_DATABASE_URL=sqlite+aiosqlite:////data/katip.db (dört eğik çizgi)",
    )
    return False


def configure(url: str | None = None) -> AsyncEngine:
    """Motoru (yeniden) kurar. Testler kendi URL'lerini verir."""
    global _engine, _sessionmaker, _pending_url
    url = url or get_settings().database_url
    if url.startswith("sqlite") and ":///" in url and ":memory:" not in url:
        db_path = Path(url.split(":///", 1)[1])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        warn_if_not_persistent(db_path)
    engine = create_async_engine(url, pool_pre_ping=not url.startswith("sqlite"))
    if url.startswith("sqlite"):
        _enable_sqlite_fk(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # Havuz ve sürücünün yürütme kilidi yalnızca bu döngüde kullanılabilir.
    try:
        _databases.set((engine, factory))
    except RuntimeError:
        # Dönen motor çağıranın döngüsünde kullanılmış olabilir; yalnız URL devredilir.
        _pending_url = url
    _engine, _sessionmaker = engine, factory
    return engine


async def init_db() -> None:
    """Tabloları oluşturur. (Şema göçleri F2'de Alembic'e taşınacak.)"""
    crypto.keys()
    engine, _ = _database()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all mevcut tablolara sütun eklemez. Üretim SQLite ve Postgres için
        # yalnız eklemeli, tekrar çalıştırılabilir geçiş; eski kayıtlar korunur.
        additions = {
            "meeting_usage": {"providers": "JSON NOT NULL DEFAULT '{}'"},
            "calendar_joins": {
                "summary": "JSON",
                "approval_snapshot": "JSON",
                "approved_account_id": "INTEGER REFERENCES panel_accounts(id) ON DELETE SET NULL",
                "auto_day": (
                    "TIMESTAMP WITH TIME ZONE"
                    if engine.dialect.name == "postgresql"
                    else "TIMESTAMP"
                ),
            },
            "calendar_join_guard": {
                "queue_limited": "BOOLEAN NOT NULL DEFAULT FALSE",
                "last_refresh_at": (
                    "TIMESTAMP WITH TIME ZONE"
                    if engine.dialect.name == "postgresql"
                    else "TIMESTAMP"
                ),
                "last_manual_refresh_at": (
                    "TIMESTAMP WITH TIME ZONE"
                    if engine.dialect.name == "postgresql"
                    else "TIMESTAMP"
                ),
            },
            "preparation_documents": {
                "digest_version": "INTEGER NOT NULL DEFAULT 1",
                "presentation": "BOOLEAN NOT NULL DEFAULT false",
            },
            "encryption_installation": {
                "encryption_started": "BOOLEAN NOT NULL DEFAULT false",
                "migration_completed": "BOOLEAN NOT NULL DEFAULT false",
            },
            "meetings": {
                "owner_account_id": "INTEGER REFERENCES panel_accounts(id) ON DELETE SET NULL",
            },
            "people": {"erasure_pending": "BOOLEAN NOT NULL DEFAULT false"},
            "worker_jobs": {"ingest_at": "TIMESTAMP"},
            "panel_accounts": {"role": "VARCHAR(16) NOT NULL DEFAULT 'admin'"},
            "meeting_segments": {"speaker_email": "VARCHAR(320)"},
            "meeting_participants": {
                "platform_id": "VARCHAR(200)",
                "identity_source": "VARCHAR(32) NOT NULL DEFAULT 'unknown'",
            },
            "admin_sessions": {
                "account_id": "INTEGER REFERENCES panel_accounts(id) ON DELETE CASCADE"
            },
        }
        for model in crypto.FIELDS:
            additions.setdefault(model.__tablename__, {})["encrypted_content"] = "JSON"
        for table, columns in additions.items():
            existing = await conn.run_sync(
                lambda sync, table=table: {c["name"] for c in inspect(sync).get_columns(table)}
            )
            for name, definition in columns.items():
                if name not in existing:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
        import uuid

        await conn.run_sync(
            lambda connection: crypto._insert(
                connection,
                crypto.EncryptionInstallation.__table__,
                {"id": 1, "installation_id": uuid.uuid4().hex},
                "id",
            )
        )
        stored = set(await conn.scalars(select(crypto.MeetingDataKey.key_id)))
        if stored:
            current, previous = crypto.keys()
            configured = {crypto.key_id(k) for k in (current, previous) if k is not None}
            if not stored.intersection(configured):
                raise crypto.DataKeyError(
                    "Şifreli toplantılar mevcut; geçerli güncel veya önceki veri anahtarı gerekli"
                )
            await conn.execute(
                update(crypto.EncryptionInstallation).values(encryption_started=True)
            )


async def dispose() -> None:
    global _engine, _sessionmaker
    state = _databases.get()
    if state is not None:
        engine, _ = state
        await engine.dispose()
        _databases.set(None)
        if _engine is engine:
            _engine, _sessionmaker = None, None


@asynccontextmanager
async def session_scope(*, write: bool = False) -> AsyncIterator[AsyncSession]:
    """İşlem sınırı: hata yoksa commit, varsa rollback."""
    _, factory = _database()
    async with factory() as session:
        try:
            if write and session.bind.dialect.name == "sqlite":
                # SQLite satır kilidi sunmaz; işlemler süreçler arasında da sıralanır.
                await session.execute(text("BEGIN IMMEDIATE"))
            yield session
            await session.commit()
        except BaseException:
            try:
                await session.rollback()
            finally:
                raise_if_cancelling()
            raise


def write_scope():
    """Okuma-değiştirme-yazma işlemini SQLite'ta baştan kilitler; okumalar beklemez."""
    return session_scope(write=True)
