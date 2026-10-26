"""Yönetici CLI: python -m katip.store.rotate_key."""

from __future__ import annotations

import asyncio

from katip.store import db
from katip.store.migration import rotate_keys


async def main() -> None:
    try:
        await db.init_db()
        count = await rotate_keys()
        print(f"Anahtar rotasyonu tamamlandı: {count} toplantı zarfı değiştirildi.")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
