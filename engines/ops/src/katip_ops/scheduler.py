"""APScheduler 3.11 asyncio registration; no threads or inbound listener."""

import logging
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from katip_ops.config import Settings


def build_scheduler(settings: Settings, runner: Any) -> AsyncIOScheduler:
    timezone = ZoneInfo("Europe/Istanbul")
    scheduler = AsyncIOScheduler(timezone=timezone)

    async def scheduled_run(job: str, source: str) -> None:
        try:
            await runner.execute(job, source=source)
        except Exception:
            # Provider exception bodies may contain input data; never log them.
            logging.getLogger(__name__).error("scheduled_job_failed", extra={"job": job})

    for name, schedule in settings.schedules.items():
        if schedule.enabled:
            scheduler.add_job(
                scheduled_run,
                CronTrigger(
                    day_of_week=schedule.day_of_week,
                    hour=schedule.hour,
                    minute=schedule.minute,
                    timezone=timezone,
                ),
                kwargs={"job": name, "source": "schedule"},
                id=name,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
            )
    return scheduler
