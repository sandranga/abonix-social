"""The long-running agent: scheduler + Telegram bot in one asyncio process."""

from __future__ import annotations

import asyncio
import logging
import signal

from app import db
from app.config import get_settings

log = logging.getLogger("abonix-social")


def build_pipeline():
    """Wires the real clients. Anything without credentials stays None and
    the pipeline reports it clearly when it is needed."""
    from app.approve import telegram
    from app.generate.text import client as claude_client
    from app.images.providers import get_provider
    from app.pipeline import Pipeline
    from app.publish.meta import get_meta
    from app.publish.storage import StorageError, get_store

    s = get_settings()
    try:
        store = get_store()
    except StorageError as e:
        log.warning("media storage not configured: %s", e)
        store = None
    p = Pipeline(
        s,
        claude=claude_client() if s.anthropic_api_key else None,
        images=get_provider(),
        store=store,
        meta=get_meta() if s.meta_access_token else None,
    )
    notifier, bot = telegram.build(p)
    if notifier:
        p.notifier = notifier
    return p, bot


async def run() -> None:
    from app.scheduler import build_scheduler

    s = get_settings()
    db.init_db()
    p, bot = build_pipeline()
    sched = build_scheduler(p)
    sched.start()
    log.info("started: generate %s, publish %s %s, dry_run=%s, targets=%s",
             s.generate_time, s.publish_time, s.timezone, s.dry_run, s.publish_targets)
    missing = [n for n, v in [("ANTHROPIC_API_KEY", s.anthropic_api_key), ("PIXABAY_API_KEY", s.pixabay_api_key or s.pexels_api_key),
                              ("TELEGRAM_BOT_TOKEN", s.telegram_bot_token),
                              ("META_ACCESS_TOKEN", s.meta_access_token)] if not v]
    if missing:
        log.warning("not configured yet: %s", ", ".join(missing))
    if bot:
        try:
            await p.notifier.info("🤖 Abonix Social a pornit.\n\n" + p.status_text())
        except Exception as e:
            log.warning("startup message failed: %s", e)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    tasks = [asyncio.create_task(stop.wait())]
    if bot:
        tasks.append(asyncio.create_task(bot.run()))
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    sched.shutdown(wait=False)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
