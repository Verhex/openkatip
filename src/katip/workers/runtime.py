from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import update

from katip.store.db import write_scope
from katip.store.models import WorkerJob


async def docker_call(*args: str, env: dict | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 15)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stdout, stderr


async def remove_container(name: str) -> None:
    code, _, stderr = await docker_call("rm", "--force", name)
    if code == 0:
        return
    if b"no such container" in stderr.lower() or b"no such object" in stderr.lower():
        return
    # Daemon unavailable / permission error is not evidence of a stopped browser.
    raise RuntimeError("Worker konteyneri kapanışı doğrulanamadı")


async def mark_local_lost(meeting_id: str) -> None:
    async with write_scope() as db:
        await db.execute(
            update(WorkerJob)
            .where(WorkerJob.meeting_id == meeting_id, WorkerJob.status == "stopping")
            .values(status="lost", updated_at=datetime.now(UTC), reason="local_process_unowned")
        )
