from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from katip.store.models import (
    AppSetting,
    Base,
    EncryptionInstallation,
    Meeting,
    MeetingDataKey,
    Person,
)
from scripts.migrate_sqlite_to_postgres import copy_database


@asynccontextmanager
async def databases(tmp_path):
    source = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source.db'}")
    target = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    try:
        async with source.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                EncryptionInstallation.__table__.insert(),
                {"id": 1, "installation_id": "old-installation", "encryption_started": True},
            )
            await conn.execute(
                Person.__table__.insert(),
                [
                    {"id": 1, "display_name": "Çalışan", "manager_id": 2},
                    {"id": 2, "display_name": "Yönetici", "manager_id": 1},
                ],
            )
            await conn.execute(
                Meeting.__table__.insert(),
                {"id": "old-meeting", "encrypted_content": {"title": "KM-base64-ciphertext"}},
            )
            await conn.execute(
                MeetingDataKey.__table__.insert(),
                {
                    "meeting_id": "old-meeting",
                    "key_id": "old-key",
                    "wrapped_key": b"\x00\xffKM\x02",
                },
            )
        yield source, target
    finally:
        await source.dispose()
        await target.dispose()


async def test_copy_every_table_preserves_ciphertext_and_self_references(tmp_path, capsys):
    async with databases(tmp_path) as (source, target):
        counts = await copy_database(source, target)
        assert set(counts) == set(Base.metadata.tables)
        assert all(a == b for a, b in counts.values())
        async with source.connect() as src, target.connect() as dst:
            for table in Base.metadata.sorted_tables:
                query = select(table).order_by(*table.primary_key)
                assert (await src.execute(query)).all() == (await dst.execute(query)).all()
            assert await dst.scalar(select(MeetingDataKey.wrapped_key)) == b"\x00\xffKM\x02"
            assert (
                await dst.scalar(select(EncryptionInstallation.installation_id))
                == "old-installation"
            )
        output = capsys.readouterr().out
        assert "kaynak=2 hedef=2" in output
        assert "Çalışan" not in output and "old-key" not in output and "ciphertext" not in output


async def test_copy_refuses_nonempty_target_and_explicit_replace_is_atomic(tmp_path):
    async with databases(tmp_path) as (source, target):
        await copy_database(source, target)
        with pytest.raises(ValueError, match="Hedef dolu"):
            await copy_database(source, target)
        # Kaynak FK hatalıysa açık değiştirme bayrağı bile hedefin eski verisini kaybetmemeli.
        async with source.begin() as conn:
            await conn.execute(Meeting.__table__.delete())
        with pytest.raises(IntegrityError):
            await copy_database(source, target, replace_target=True)
        async with target.connect() as conn:
            assert await conn.scalar(select(func.count()).select_from(Meeting)) == 1
            assert await conn.scalar(select(func.count()).select_from(Person)) == 2


async def test_explicit_replace_and_same_source_guard(tmp_path):
    async with databases(tmp_path) as (source, target):
        await copy_database(source, target)
        await copy_database(source, target, replace_target=True)
        with pytest.raises(ValueError, match="aynı"):
            await copy_database(source, source)


async def test_copy_refuses_incomplete_source_schema(tmp_path):
    source = create_async_engine("sqlite+aiosqlite:///:memory:")
    target = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with source.begin() as conn:
            await conn.run_sync(AppSetting.__table__.create)
        with pytest.raises(ValueError, match="şeması"):
            await copy_database(source, target)
    finally:
        await source.dispose()
        await target.dispose()


async def test_count_mismatch_rolls_back_entire_copy(tmp_path, capsys):
    async with databases(tmp_path) as (source, target):
        async with target.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                text(
                    "CREATE TRIGGER skip_person BEFORE INSERT ON people WHEN NEW.id=2 "
                    "BEGIN SELECT RAISE(IGNORE); END"
                )
            )
        with pytest.raises(ValueError, match="Satır sayıları"):
            await copy_database(source, target)
        assert "people: kaynak=2 hedef=1" in capsys.readouterr().out
        async with target.connect() as conn:
            for table in Base.metadata.sorted_tables:
                assert await conn.scalar(select(func.count()).select_from(table)) == 0


async def test_copy_streams_across_batch_boundary(tmp_path):
    async with databases(tmp_path) as (source, target):
        async with source.begin() as conn:
            await conn.execute(
                Person.__table__.insert(),
                [{"id": n, "display_name": "Çalışan", "manager_id": 2} for n in range(10, 530)],
            )
        counts = await copy_database(source, target)
        assert counts["people"] == (522, 522)


def test_cli_failure_never_prints_connection_secret(tmp_path, monkeypatch, capsys):
    from scripts.migrate_sqlite_to_postgres import main

    monkeypatch.setattr(
        "sys.argv",
        [
            "migrate_sqlite_to_postgres.py",
            "--source",
            str(tmp_path / "absent.db"),
            "--target-url",
            "postgresql+asyncpg://katip:private-password@unused/katip",
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "private-password" not in capsys.readouterr().err
