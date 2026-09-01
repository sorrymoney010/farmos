"""Reconciliation worker stub."""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def run():
    logger.info("reconciliation_worker_started")
    while True:
        await asyncio.sleep(30)
        logger.debug("reconciliation_tick")


if __name__ == "__main__":
    asyncio.run(run())
