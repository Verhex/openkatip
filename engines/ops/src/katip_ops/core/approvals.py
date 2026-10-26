"""Owner-only, expiring, digest-bound approvals. External effects belong to M2."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue

from katip_ops.core._db import connect
from katip_ops.core.contract import ProposedAction


@dataclass(frozen=True)
class Approval:
    id: str
    task_id: str
    run_id: str
    action: ProposedAction
    original_digest: str
    digest: str
    status: str
    expires_at: float
    edited: bool


class Approvals:
    def __init__(
        self,
        path: str | Path,
        owner_slack_id: str,
        ttl_hours: float = 72,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not owner_slack_id or ttl_hours <= 0:
            raise ValueError("Owner and positive approval TTL are required")
        self.db = connect(path)
        self.owner_slack_id = owner_slack_id
        self.ttl_seconds = ttl_hours * 3600
        self.clock = clock

    def close(self) -> None:
        self.db.close()

    def pending_count(self) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='pending' AND expires_at > ?",
            (self.clock(),),
        ).fetchone()
        return int(row[0])

    def summary(self, since: float = 0) -> dict[str, int]:
        return {
            row["status"]: int(row["total"])
            for row in self.db.execute(
                "SELECT status, COUNT(*) AS total FROM approvals "
                "WHERE created_at>=? GROUP BY status",
                (since,),
            )
        }

    def create(self, action: ProposedAction, task_id: str, run_id: str) -> str:
        approval_id = str(uuid4())
        now = self.clock()
        snapshot = action.model_dump_json()
        with self.db:
            self.db.execute(
                """INSERT INTO approvals
                (id, task_id, run_id, original_action, action, original_digest, digest,
                 created_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    approval_id,
                    task_id,
                    run_id,
                    snapshot,
                    snapshot,
                    action.digest,
                    action.digest,
                    now,
                    now + self.ttl_seconds,
                ),
            )
        return approval_id

    def get(self, approval_id: str) -> Approval:
        row = self.db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise ValueError("Onay kaydı bulunamadı.")
        action = ProposedAction.model_validate_json(row["action"])
        if action.digest != row["digest"]:
            raise ValueError("İşlem özeti onay kaydıyla eşleşmiyor.")
        return Approval(
            row["id"],
            row["task_id"],
            row["run_id"],
            action,
            row["original_digest"],
            row["digest"],
            row["status"],
            row["expires_at"],
            bool(row["edited"]),
        )

    def decision(
        self,
        approval_id: str,
        decision: Literal["approved", "rejected"],
        actor_slack_id: str,
        edited_payload: dict[str, JsonValue] | None = None,
    ) -> Approval:
        if actor_slack_id != self.owner_slack_id:
            raise PermissionError("Bu onayı yalnızca yetkili kullanıcı verebilir.")
        if decision not in ("approved", "rejected"):
            raise ValueError("Geçersiz onay kararı.")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            approval = self.get(approval_id)
            if approval.status != "pending":
                raise ValueError("Bu onay daha önce kullanıldı.")
            now = self.clock()
            if now >= approval.expires_at:
                raise ValueError("Onayın süresi doldu.")
            action = approval.action
            if edited_payload is not None:
                action = ProposedAction.model_validate(
                    {
                        **action.model_dump(),
                        "payload": edited_payload,
                    }
                )
            self.db.execute(
                """UPDATE approvals SET action=?, digest=?, status=?, actor=?,
                decided_at=?, edited=? WHERE id=?""",
                (
                    action.model_dump_json(),
                    action.digest,
                    decision,
                    actor_slack_id,
                    now,
                    int(edited_payload is not None),
                    approval_id,
                ),
            )
        return self.get(approval_id)

    def mark_applied(self, approval_id: str) -> None:
        """Record completion idempotently; this method does not execute the action."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            approval = self.get(approval_id)
            if approval.status == "applied":
                return
            now = self.clock()
            if approval.status != "approved" or now >= approval.expires_at:
                raise ValueError("İşlem için geçerli bir onay bulunmuyor.")
            self.db.execute(
                "UPDATE approvals SET status='applied', applied_at=? WHERE id=?",
                (now, approval_id),
            )
