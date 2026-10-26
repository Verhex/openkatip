from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from katip.api.security import extend_bot_token, register_bot_token_hash, revoke_bot_token
from katip.async_utils import LoopLocal
from katip.config import get_settings
from katip.store import repo
from katip.store.db import session_scope, write_scope
from katip.store.models import WorkerJob

OPEN = ("queued", "running", "stopping", "lost")
_claim_lock = LoopLocal(asyncio.Lock)


def utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def get(meeting_id: str) -> WorkerJob | None:
    async with session_scope() as db:
        return await db.get(WorkerJob, meeting_id)


async def listing(*, open_only: bool = False) -> list[WorkerJob]:
    async with session_scope() as db:
        query = select(WorkerJob).order_by(WorkerJob.created_at.desc())
        if open_only:
            query = query.where(WorkerJob.status.in_(OPEN))
        return list(await db.scalars(query.limit(500)))


async def count_open() -> int:
    async with session_scope() as db:
        return int(
            await db.scalar(
                select(func.count()).select_from(WorkerJob).where(WorkerJob.status.in_(OPEN))
            )
            or 0
        )


async def enqueue(meeting_id: str, spec: dict, max_minutes: int) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    if settings.bot_backend == "remote" and (
        not settings.worker_manager_keys
        or any(len(key) < 32 for key in settings.worker_manager_keys.values())
    ):
        raise RuntimeError("Uzak worker kimliği tanımlı değil")
    async with write_scope() as db:
        db.add(
            WorkerJob(
                meeting_id=meeting_id,
                backend=settings.bot_backend,
                spec=spec,
                status="queued",
                deadline=now + timedelta(seconds=settings.worker_queue_timeout_s),
            )
        )


async def started_local(meeting_id: str, max_minutes: int) -> None:
    now = datetime.now(UTC)
    async with write_scope() as db:
        await db.execute(
            update(WorkerJob)
            .where(
                WorkerJob.meeting_id == meeting_id,
                WorkerJob.status == "queued",
            )
            .values(
                status="running",
                updated_at=now,
                deadline=now + timedelta(minutes=max_minutes, seconds=120),
            )
        )


async def finish_local(meeting_id: str, *, failed: bool = False) -> None:
    async with write_scope() as db:
        row = await db.get(WorkerJob, meeting_id)
        if row and row.status in OPEN:
            row.status = (
                "cancelled" if row.status == "stopping" else ("failed" if failed else "completed")
            )
            row.updated_at = datetime.now(UTC)
    revoke_bot_token(meeting_id)


async def resolve_unknown(meeting_id: str) -> bool:
    """Trusted admin attestation, distinct from a worker's machine acknowledgement."""
    async with write_scope() as db:
        result = await db.execute(
            update(WorkerJob)
            .where(WorkerJob.meeting_id == meeting_id, WorkerJob.status == "lost")
            .values(
                status="closed_manually",
                reason="admin_verified_stop",
                token_hash=None,
                updated_at=datetime.now(UTC),
            )
        )
        resolved = result.rowcount == 1
    if resolved:
        revoke_bot_token(meeting_id)
        await repo.audit("admin", "worker_manually_closed", meeting_id=meeting_id)
    return resolved


async def request_cancel(meeting_id: str, reason: str = "operator_cancelled") -> str | None:
    async with write_scope() as db:
        # Also serialize with claim/heartbeat on PostgreSQL.
        row = await db.scalar(
            select(WorkerJob).where(WorkerJob.meeting_id == meeting_id).with_for_update()
        )
        if row is None:
            return None
        if row.status in ("queued", "running", "stopping"):
            row.status = "cancelled" if row.status == "queued" else "stopping"
            row.reason = reason
            row.token_hash = None
            row.updated_at = datetime.now(UTC)
        status = row.status
    revoke_bot_token(meeting_id)
    return status


async def claim(worker_id: str) -> tuple[WorkerJob, str] | None:
    """CAS is the ownership boundary, including a lost claim HTTP response."""
    settings = get_settings()
    now = datetime.now(UTC)
    async with _claim_lock.get(), write_scope() as db:
        occupied = await db.scalar(
            select(WorkerJob.meeting_id)
            .where(WorkerJob.worker_id == worker_id, WorkerJob.status.in_(OPEN))
            .limit(1)
        )
        if occupied:
            return None
        row = await db.scalar(
            select(WorkerJob)
            .where(
                WorkerJob.backend == "remote",
                WorkerJob.status == "queued",
                WorkerJob.deadline > now,
            )
            .order_by(WorkerJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        lease_id = secrets.token_hex(24)
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        values = dict(
            status="running",
            worker_id=worker_id,
            lease_id=lease_id,
            token_hash=digest,
            lease_until=now + timedelta(seconds=settings.worker_lease_s),
            heartbeat_at=now,
            updated_at=now,
            deadline=now + timedelta(minutes=int(row.spec["max_minutes"]), seconds=120),
        )
        result = await db.execute(
            update(WorkerJob)
            .where(
                WorkerJob.meeting_id == row.meeting_id,
                WorkerJob.status == "queued",
            )
            .values(**values)
        )
        if result.rowcount != 1:
            return None
        await db.refresh(row)
    register_bot_token_hash(row.meeting_id, digest, expires=utc(row.lease_until).timestamp())
    return row, token


async def heartbeat(meeting_id: str, worker_id: str, lease_id: str) -> bool:
    now = datetime.now(UTC)
    until = now + timedelta(seconds=get_settings().worker_lease_s)
    async with write_scope() as db:
        result = await db.execute(
            update(WorkerJob)
            .where(
                WorkerJob.meeting_id == meeting_id,
                WorkerJob.worker_id == worker_id,
                WorkerJob.lease_id == lease_id,
                WorkerJob.status == "running",
                WorkerJob.lease_until > now,
                WorkerJob.deadline > now,
            )
            .values(
                lease_until=until,
                heartbeat_at=now,
                updated_at=now,
            )
        )
        renewed = result.rowcount == 1
    if renewed:
        extend_bot_token(meeting_id, until.timestamp())
    return renewed


async def finish(meeting_id: str, worker_id: str, lease_id: str, exit_code: int) -> bool:
    async with write_scope() as db:
        row = await db.scalar(
            select(WorkerJob)
            .where(
                WorkerJob.meeting_id == meeting_id,
                WorkerJob.worker_id == worker_id,
                WorkerJob.lease_id == lease_id,
            )
            .with_for_update()
        )
        if row is None:
            return False
        # Idempotent acknowledgement after a response was lost.
        if row.status in ("completed", "failed", "cancelled"):
            return True
        row.status = (
            "cancelled"
            if row.status in ("stopping", "lost")
            else ("completed" if exit_code == 0 else "failed")
        )
        row.token_hash = None
        row.updated_at = datetime.now(UTC)
        state = row.status
    revoke_bot_token(meeting_id)
    from katip.pipeline.session import registry

    await registry.drop(meeting_id)
    await repo.finish_meeting(meeting_id, "ended" if state == "completed" else state)
    return True


def public_status(row: WorkerJob) -> dict:
    # No meeting URL, token hash, lease identifier or job spec in panel listings.
    return {
        "session_id": row.meeting_id,
        "backend": row.backend,
        "status": row.status,
        "worker_id": row.worker_id,
        "reason": row.reason,
        "heartbeat_at": row.heartbeat_at,
        "ingest_at": row.ingest_at,
        "updated_at": row.updated_at,
        "stop_confirmed": row.status in ("completed", "failed", "cancelled"),
        "manually_closed": row.status == "closed_manually",
    }


async def recover() -> None:
    """Queued jobs survive; active calls are fenced, never silently rejoined."""
    from katip.pipeline.session import registry

    for row in await listing(open_only=True):
        if (
            row.status == "queued"
            and row.backend == "remote"
            and utc(row.deadline) > datetime.now(UTC)
        ):
            if registry.get(row.meeting_id) is None:
                spec = row.spec
                session = registry.create(
                    row.meeting_id,
                    source_lang=spec["source_lang"],
                    target_lang=spec["target_lang"],
                    mode=spec["mode"],
                )
                from katip.identity import ParticipantIdentity

                if spec.get("participants"):
                    await session.add_identities(
                        [ParticipantIdentity(**p) for p in spec["participants"]], "panel"
                    )
        else:
            await request_cancel(row.meeting_id, "api_restarted")
            await repo.finish_meeting(row.meeting_id, "interrupted")


async def sweep() -> None:
    from katip.bot.supervisor import supervisor
    from katip.pipeline.session import registry

    now = datetime.now(UTC)
    for row in await listing(open_only=True):
        if row.status == "lost":
            continue
        expired = utc(row.deadline) <= now
        browser_lost = row.status == "running" and (
            (
                row.ingest_at is not None
                and utc(row.ingest_at) + timedelta(seconds=get_settings().bot_disconnect_timeout_s)
                <= now
            )
            or (
                row.ingest_at is None
                and utc(row.deadline) - timedelta(minutes=int(row.spec["max_minutes"])) <= now
            )
        )
        if row.backend == "remote":
            expired = (
                expired or browser_lost or bool(row.lease_until and utc(row.lease_until) <= now)
            )
            if expired and row.status != "stopping":
                await request_cancel(
                    row.meeting_id,
                    "ingest_lost"
                    if browser_lost
                    else ("lease_expired" if row.lease_until else "queue_expired"),
                )
                await registry.drop(row.meeting_id)
                await repo.finish_meeting(row.meeting_id, "interrupted")
            # Lost is explicit: no proof the remote container has stopped. Never retry.
            if (
                row.status == "stopping"
                and row.lease_until
                and utc(row.lease_until) + timedelta(seconds=15) <= now
            ):
                async with write_scope() as db:
                    await db.execute(
                        update(WorkerJob)
                        .where(
                            WorkerJob.meeting_id == row.meeting_id, WorkerJob.status == "stopping"
                        )
                        .values(status="lost", updated_at=now, token_hash=None)
                    )
                revoke_bot_token(row.meeting_id)
                await registry.drop(row.meeting_id)
        elif expired or browser_lost or row.status == "stopping":
            await request_cancel(
                row.meeting_id, "deadline_expired" if expired else row.reason or "cancelled"
            )
            await supervisor.stop(row.meeting_id)
            await registry.drop(row.meeting_id)


async def touch_ingest(meeting_id: str) -> None:
    async with write_scope() as db:
        await db.execute(
            update(WorkerJob)
            .where(WorkerJob.meeting_id == meeting_id, WorkerJob.status == "running")
            .values(ingest_at=datetime.now(UTC))
        )


async def run_forever() -> None:
    import logging

    while True:
        try:
            await sweep()
        except Exception:
            logging.getLogger(__name__).exception("Worker job health check failed")
        await asyncio.sleep(5)
