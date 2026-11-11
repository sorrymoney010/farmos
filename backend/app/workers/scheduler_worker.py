"""Scheduler worker (spec §16, §62): runs a scheduler tick every
SCHEDULER_INTERVAL_SECONDS. Each tick is deterministic and auditable; every
assignment is its own transaction with row locks, so running several workers
cannot double-book a device.

Run: python -m app.workers.scheduler_worker
"""

import asyncio

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.logging import get_logger
from app.services import scheduler_service as sched

logger = get_logger(__name__)


async def tick_once() -> dict:
    async with AsyncSessionLocal() as db:
        report = await sched.scheduler_tick(db)
        await db.commit()
        return report


async def run(stop: asyncio.Event | None = None) -> None:
    logger.info("scheduler_worker_started", interval=settings.SCHEDULER_INTERVAL_SECONDS)
    while stop is None or not stop.is_set():
        try:
            r = await tick_once()
            if r["dispatched"] or r["expired"]:
                logger.info("scheduler_tick", dispatched=len(r["dispatched"]), waiting=len(r["waiting"]),
                            expired=len(r["expired"]))
        except Exception:  # never let one bad tick kill the worker
            logger.exception("scheduler_tick_failed")
        await asyncio.sleep(settings.SCHEDULER_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run())
