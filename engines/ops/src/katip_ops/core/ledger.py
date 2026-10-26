"""Minimal run metadata and costs; task inputs and prompt text are never persisted."""

import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from katip_ops.core._db import connect
from katip_ops.core.contract import Cost, FrozenModel, Status, Task


class RunSummary(FrozenModel):
    run_id: str
    task_id: str
    task_type: str
    executor: str
    status: str
    started_at: float
    ended_at: float | None
    prompt_hash: str
    prompt_bytes: int
    prompt_chars: int
    cost: Cost | None


class Ledger:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.db = connect(path)
        self.clock = clock

    def close(self) -> None:
        self.db.close()

    def record_start(self, run_id: str, task: Task, executor: str, prompt: str) -> None:
        encoded = prompt.encode("utf-8")
        with self.db:
            self.db.execute(
                """INSERT INTO runs
                (run_id, task_id, task_type, data_class, executor, status, started_at,
                 prompt_hash, prompt_bytes, prompt_chars)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)""",
                (
                    run_id,
                    task.id,
                    task.type,
                    task.data_class,
                    executor,
                    self.clock(),
                    hashlib.sha256(encoded).hexdigest(),
                    len(encoded),
                    len(prompt),
                ),
            )

    def record_end(self, run_id: str, status: Status, cost: Cost) -> None:
        with self.db:
            cursor = self.db.execute(
                """UPDATE runs SET status=?, ended_at=?, cost=?, usd=?
                WHERE run_id=? AND ended_at IS NULL""",
                (
                    status,
                    self.clock(),
                    cost.model_dump_json(),
                    cost.usd,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Run is unknown or already completed")

    def record_executor(self, run_id: str, executor: str) -> None:
        """Attribute fallback usage to the adapter that actually answered."""
        with self.db:
            self.db.execute(
                "UPDATE runs SET executor=? WHERE run_id=? AND ended_at IS NULL", (executor, run_id)
            )

    def month_to_date_usd(self) -> float:
        """Sum known costs since UTC month start; unknown costs remain visible in summaries."""
        now = self.clock()
        start = (
            datetime.fromtimestamp(now, UTC)
            .replace(
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            .timestamp()
        )
        row = self.db.execute(
            """SELECT COALESCE(SUM(usd), 0) FROM runs
            WHERE started_at >= ? AND started_at <= ?""",
            (start, now),
        ).fetchone()
        return float(row[0])

    def summaries(self, since: float = 0, until: float | None = None) -> list[RunSummary]:
        rows = self.db.execute(
            """SELECT * FROM runs WHERE started_at >= ? AND started_at <= ?
            ORDER BY started_at, run_id""",
            (since, until if until is not None else self.clock()),
        )
        return [
            RunSummary(
                run_id=row["run_id"],
                task_id=row["task_id"],
                task_type=row["task_type"],
                executor=row["executor"],
                status=row["status"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                prompt_hash=row["prompt_hash"],
                prompt_bytes=row["prompt_bytes"],
                prompt_chars=row["prompt_chars"],
                cost=Cost.model_validate_json(row["cost"]) if row["cost"] else None,
            )
            for row in rows
        ]
