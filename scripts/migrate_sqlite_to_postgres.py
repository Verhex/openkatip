"""Durdurulmuş SQLite kaynağını içerik çözmeden boş hedefe kopyalar.

Hedef URL KATIP_DATABASE_URL ortamından da okunabilir. Uygulama açılışı kullanılmaz:
init_db yeni kurulum kimliği üretir; burada yalnız metadata.create_all çalışır.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from katip.store.models import Base


@asynccontextmanager
async def _snapshot(engine: AsyncEngine):
    async with engine.connect() as conn:
        try:
            # SQLite SELECT normalde BEGIN başlatmaz; tutarlı anlık görüntü gerekir.
            await conn.execute(text("PRAGMA query_only=ON"))
            await conn.execute(text("BEGIN"))
            yield conn
        finally:
            await conn.rollback()
            await conn.execute(text("PRAGMA query_only=OFF"))


async def copy_database(
    source: AsyncEngine, target: AsyncEngine, *, replace_target: bool = False
) -> dict[str, tuple[int, int]]:
    """Tek hedef işlemi: tüm tablolar, FK sırası, sayım ve PostgreSQL dizileri.

    Core işlemleri ORM şifreleme olaylarını çağırmaz; BLOB ve JSON zarfları korunur.
    Kaynak ve hedefe başka süreçler yazmamalıdır. Hata hedefi geri alır.
    """
    if source.dialect.name != "sqlite":
        raise ValueError("Kaynak SQLite olmalıdır")
    if source is target or (
        target.dialect.name == "sqlite"
        and source.url.database != ":memory:"
        and await asyncio.to_thread(Path(source.url.database).resolve)
        == await asyncio.to_thread(Path(target.url.database).resolve)
    ):
        raise ValueError("Kaynak ve hedef aynı olamaz")
    tables = Base.metadata.sorted_tables
    counts = {}
    async with _snapshot(source) as src, target.connect() as dst:
        source_tables = await src.run_sync(lambda c: set(inspect(c).get_table_names()))
        if source_tables != set(Base.metadata.tables):
            raise ValueError("Kaynak şeması model tablolarıyla eşleşmiyor; aynı sürümü kullanın")
        if target.dialect.name == "sqlite":
            await dst.execute(text("PRAGMA foreign_keys=ON"))
            await dst.execute(text("BEGIN IMMEDIATE"))
        else:
            await dst.begin()
        try:
            await dst.run_sync(Base.metadata.create_all)
            if target.dialect.name == "postgresql":
                names = ", ".join(dst.dialect.identifier_preparer.quote(t.name) for t in tables)
                await dst.execute(text(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE"))
            occupied = any(
                [await dst.scalar(select(func.count()).select_from(table)) for table in tables]
            )
            if occupied and not replace_target:
                raise ValueError("Hedef dolu; değiştirmek için --replace-target gereklidir")
            # Öz başvurular (people.manager_id) sıralamadan bağımsız olarak ikinci geçişte yazılır.
            self_refs = {
                t.name: [c for c in t.c if any(f.column.table is t for f in c.foreign_keys)]
                for t in tables
            }
            if replace_target:
                for table in reversed(tables):
                    for column in self_refs[table.name]:
                        await dst.execute(update(table).values({column.name: None}))
                    await dst.execute(table.delete())
            deferred = []
            for table in tables:
                total = int(await src.scalar(select(func.count()).select_from(table)))
                result = await src.stream(select(table))
                async for batch in result.mappings().partitions(500):
                    rows = []
                    for original in batch:
                        row = dict(original)
                        # SQLite saat dilimini saklamaz; uygulama zamanları UTC'dir.
                        for column in table.c:
                            value = row[column.name]
                            if isinstance(value, datetime) and getattr(
                                column.type, "timezone", False
                            ):
                                if value.tzinfo is None:
                                    row[column.name] = value.replace(tzinfo=UTC)
                        refs = {c.name: row[c.name] for c in self_refs[table.name]}
                        if any(v is not None for v in refs.values()):
                            pk = {c.name: row[c.name] for c in table.primary_key}
                            deferred.append((table, pk, refs))
                            row.update(dict.fromkeys(refs))
                        rows.append(row)
                    if rows:
                        await dst.execute(table.insert(), rows)
                copied = int(await dst.scalar(select(func.count()).select_from(table)))
                counts[table.name] = (total, copied)
                print(f"{table.name}: kaynak={total} hedef={copied}")
                if total != copied:
                    raise ValueError("Satır sayıları eşleşmiyor; hedef işlemi geri alındı")
            for table, pk, refs in deferred:
                condition = [table.c[k] == v for k, v in pk.items()]
                await dst.execute(update(table).where(*condition).values(refs))
            if target.dialect.name == "postgresql":
                for table in tables:
                    for column in table.primary_key:
                        sequence = await dst.scalar(
                            text("SELECT pg_get_serial_sequence(:t, :c)"),
                            {"t": table.name, "c": column.name},
                        )
                        if sequence:
                            maximum = await dst.scalar(select(func.max(column)))
                            await dst.execute(
                                text("SELECT setval(CAST(:s AS regclass), :v, :called)"),
                                {
                                    "s": sequence,
                                    "v": max(maximum or 1, 1),
                                    "called": maximum is not None,
                                },
                            )
            await dst.commit()
        except BaseException:
            await dst.rollback()
            raise
    return counts


async def _run(args) -> None:
    if not await asyncio.to_thread(args.source.is_file):
        raise ValueError("Kaynak dosya bulunamadı")
    if not args.target_url:
        raise ValueError("KATIP_DATABASE_URL veya --target-url gerekli")
    path = await asyncio.to_thread(args.source.resolve)
    source = create_async_engine("sqlite+aiosqlite:///" + path.as_posix())
    target = create_async_engine(args.target_url, hide_parameters=True)
    try:
        await copy_database(source, target, replace_target=args.replace_target)
    finally:
        await source.dispose()
        await target.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite → PostgreSQL veri kopyalama")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target-url", default=os.environ.get("KATIP_DATABASE_URL", ""))
    parser.add_argument(
        "--replace-target", action="store_true", help="Hedefteki tüm model satırlarını sil"
    )
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except Exception as exc:
        # Sürücü hataları SQL parametrelerinde içerik/URL taşıyabilir; metnini yazmayın.
        parser.exit(1, f"Taşıma başarısız ({type(exc).__name__}); hedefi ve şemayı denetleyin.\n")


if __name__ == "__main__":
    main()
