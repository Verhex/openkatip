"""Saklama ve imha: süresi dolan toplantı verileri ve teşhis dosyaları silinir.

KVKK "Kişisel Verilerin Silinmesi, Yok Edilmesi veya Anonim Hâle Getirilmesi Hakkında
Yönetmelik" periyodik imhayı (en geç 6 ayda bir) ve silme kayıtlarının saklanmasını ister.
Bu iş günde bir çalışır:
- `KATIP_RETENTION_DAYS` (varsayılan 365) günden eski toplantılar: transkript, notlar,
  katılımcılar, kullanım kaydı (veritabanında CASCADE) ve rapor/ekran görüntüsü dosyaları.
- `KATIP_DEBUG_RETENTION_DAYS` (varsayılan 7) günden eski teşhis dosyaları (sayfa HTML'i ve
  ekran görüntüsü; kişisel veri içerebilir).
Kişisel veri içermeyen bir silme kaydı denetim loguna yazılır. Ses izleri kişiye bağlıdır;
toplantıyla silinmez, "beni unut" ya da yönetici silmesiyle gider.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from katip.config import get_settings
from katip.logging import get_logger
from katip.store import crypto, repo
from katip.store.db import session_scope
from katip.store.models import Meeting

log = get_logger(__name__)

_FIRST_RUN_DELAY_S = 300
_INTERVAL_S = 24 * 3600


def _delete_meeting_files(data: Path, ids: list[str], files: list[str]) -> int:
    deleted = 0
    for path in files:
        with contextlib.suppress(OSError):
            Path(path).unlink()
            deleted += 1
    for meeting_id in ids:
        for kind in ("reports", "screenshots", "presentations"):
            base = (data / kind).resolve()
            folder = (base / meeting_id).resolve()
            if folder.parent == base:
                shutil.rmtree(folder, ignore_errors=True)
        base = (data / "debug").resolve()
        # Nokta/alt çizgi/tire ile ayrılmış dosyalar ve toplantı alt dizini.
        for path in base.glob("*"):
            if path.name == meeting_id or any(
                path.name.startswith(meeting_id + sep) for sep in (".", "_", "-")
            ):
                if path.resolve().parent != base or path.is_symlink():
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                    deleted += 1
    return deleted


def _delete_old_debug(data: Path, days: int) -> int:
    limit = time.time() - days * 86400
    deleted = 0
    for path in (data / "debug").glob("*"):
        with contextlib.suppress(OSError):
            if path.is_file() and path.stat().st_mtime < limit:
                path.unlink()
                deleted += 1
    return deleted


async def purge_once() -> dict:
    settings = get_settings()
    data = Path(settings.data_dir)
    result = {"meetings": 0, "files": 0, "debug_files": 0}
    async with session_scope() as session:
        live_ids = set(await session.scalars(select(Meeting.id)))
    await asyncio.to_thread(crypto.cleanup_orphan_locks, live_ids)
    if settings.debug_retention_days > 0:
        result["debug_files"] = await asyncio.to_thread(
            _delete_old_debug, data, settings.debug_retention_days
        )
    if settings.retention_days > 0:
        cutoff = datetime.now(UTC) - timedelta(days=settings.retention_days)
        ids, files = await repo.purge_meetings_before(cutoff)
        result["meetings"] = len(ids)
        result["files"] = len(files)
    if any(result.values()):
        await repo.audit("system", "retention_purge", **result)
        log.info("saklama süresi dolan veriler silindi", **result)
    return result


async def run_forever() -> None:
    await asyncio.sleep(_FIRST_RUN_DELAY_S)
    while True:
        try:
            await purge_once()
        except Exception as exc:
            log.exception("saklama/imha işi başarısız", error=str(exc))
        await asyncio.sleep(_INTERVAL_S)
