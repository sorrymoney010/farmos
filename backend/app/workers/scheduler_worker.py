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


async def deployments_tick_once() -> dict:
    """Sprint 16: create due customer-deployment runs, release reservations of
    failed runs, deliver customer webhooks."""
    from app.services.developer import deployments
    async with AsyncSessionLocal() as db:
        report = await deployments.tick(db)
        await db.commit()
        return report


async def tick_once() -> dict:
    try:
        dep = await deployments_tick_once()
        if dep.get("runs_created") or dep.get("resumed") or (dep.get("settlement") or {}).get("released"):
            logger.info("deployments_tick", **{k: v for k, v in dep.items() if not isinstance(v, dict)})
    except Exception:  # a bad deployment tick must not block dispatching
        logger.exception("deployments_tick_failed")
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
