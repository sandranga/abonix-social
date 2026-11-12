from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.pipeline import Pipeline

log = logging.getLogger(__name__)


def build_scheduler(p: Pipeline) -> AsyncIOScheduler:
    s = p.s
    sched = AsyncIOScheduler(timezone=s.tz)

    async def daily_generate():
        if p.paused:
            log.info("paused: skipping daily generation")
            return
        try:
            await p.generate_for(p.today())
        except Exception:
            pass  # already alerted inside generate_for

    async def publish_tick():
        try:
            await p.publish_due()
        except Exception as e:
            log.exception("publish tick failed")
            await p.notifier.alert(f"Eroare în ciclul de publicare: {e}")

    sched.add_job(daily_generate, CronTrigger(hour=s.generate_time.hour, minute=s.generate_time.minute),
                  id="generate", misfire_grace_time=3 * 3600, coalesce=True)
    sched.add_job(publish_tick, IntervalTrigger(minutes=1), id="publish", coalesce=True, max_instances=1)
    sched.add_job(p.health_check, CronTrigger(hour=7, minute=30), id="health", coalesce=True)
    return sched
