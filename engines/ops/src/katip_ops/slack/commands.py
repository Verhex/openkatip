"""Deterministic commands. No free-form chat or model execution."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from katip_ops.config import Settings
from katip_ops.core.approvals import Approvals
from katip_ops.core.ledger import Ledger

logger = logging.getLogger(__name__)
REFUSAL = "Bu komutu yalnızca yetkili kullanıcı kullanabilir."
HELP = "Komutlar: durum · çalıştır <pipeline|icerik|geo|cuma> · fikir <metin> · yardım"
JOBS = {"pipeline", "icerik", "geo", "cuma"}
STATUS = {
    "running": "çalışıyor",
    "done": "tamamlandı",
    "needs_approval": "onay bekliyor",
    "failed": "başarısız",
    "rejected_by_policy": "politika reddetti",
}


class JobRunner(Protocol):
    async def enqueue(self, job: str, *, requested_by: str) -> str: ...


class Commands:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        approvals: Approvals,
        runner: JobRunner,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.approvals = approvals
        self.runner = runner
        self.clock = clock or (lambda: datetime.now(UTC))

    def is_owner(self, user_id: str) -> bool:
        if user_id != self.settings.owner_slack_id:
            logger.warning("slack_owner_refused", extra={"actor": user_id})
            return False
        return True

    async def execute(self, user_id: str, text: str) -> str:
        if not self.is_owner(user_id):
            return REFUSAL
        command, _, argument = text.strip().partition(" ")
        argument = argument.strip()
        if command == "yardım" and not argument:
            return HELP
        if command == "durum" and not argument:
            return self._status()
        if command == "çalıştır" and argument in JOBS:
            await self.runner.enqueue(argument, requested_by=user_id)
            return f"İş kuyruğa alındı: {argument}."
        if command == "fikir" and argument:
            self._save_idea(argument)
            return "Fikir kaydedildi."
        return HELP

    def _status(self) -> str:
        rows = self.ledger.summaries()
        now = datetime.fromtimestamp(self.ledger.clock(), UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        unknown = any(
            row.started_at >= start and (row.cost is None or row.cost.usd is None) for row in rows
        )
        cost = "bilinmiyor" if unknown else f"{self.ledger.month_to_date_usd():.4f} USD"
        recent = [
            f"• {row.task_type}: {STATUS.get(row.status, 'bilinmiyor')}" for row in rows[-5:][::-1]
        ]
        return (
            "Son çalışmalar:\n"
            + ("\n".join(recent) or "Henüz çalışma yok.")
            + f"\nBekleyen onay: {self.approvals.pending_count()}"
            + f"\nBu ayın maliyeti: {cost}"
        )

    def _save_idea(self, text: str) -> Path:
        if len(text) > 10000:
            raise ValueError("Fikir metni çok uzun.")
        now = self.clock().astimezone(ZoneInfo(self.settings.timezone))
        root = self.settings.ops_dir.resolve()
        destination = root / "content" / "ideas" / f"{now:%Y-%m-%d}.md"
        # The name comes solely from the clock. Resolve existing symlinks/junctions too.
        if not destination.resolve().is_relative_to(root):
            raise ValueError("Fikir klasörü çalışma dizini dışında olamaz.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.resolve().is_relative_to(root):
            raise ValueError("Fikir klasörü çalışma dizini dışında olamaz.")
        single_line = " ".join(text.splitlines())
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(f"- {now.isoformat(timespec='seconds')} {single_line}\n")
        return destination
