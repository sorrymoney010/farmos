"""Verification / lease worker.

Periodically expires overdue job leases (requeue or dead-letter, spec §69-§71)
and resolves primaries whose redundant sample can no longer complete.
"""

import asyncio
import logging
import os

from app.db.session import AsyncSessionLocal
from app.services import lease_service as leases

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = float(os.getenv("LEASE_SWEEP_INTERVAL_SECONDS", "10"))


async def tick() -> int:
    async with AsyncSessionLocal() as db:
        swept = await leases.sweep_expired(db, limit=500)
    if swept:
        logger.info("lease_sweep expired=%d", len(swept))
    return len(swept)


async def run():
    logger.info("verification_worker_started interval=%ss", INTERVAL_SECONDS)
    while True:
        try:
            await tick()
        except Exception:  # keep the worker alive; next tick retries
            logger.exception("lease_sweep_failed")
        await asyncio.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
