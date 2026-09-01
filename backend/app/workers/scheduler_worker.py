"""Scheduler worker stub."""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def run():
    logger.info("scheduler_worker_started")
    while True:
        await asyncio.sleep(5)
        logger.debug("scheduler_tick")


if __name__ == "__main__":
    asyncio.run(run())
