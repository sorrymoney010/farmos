"""Adapter registry. Adding an adapter = add an instance here (+ certification)."""

from __future__ import annotations

from app.adapters.acurast_sim import AcurastSimAdapter
from app.adapters.base import RevenueAdapter
from app.adapters.internal_compute import InternalComputeAdapter

ADAPTERS: dict[str, RevenueAdapter] = {a.id: a for a in (InternalComputeAdapter(), AcurastSimAdapter())}

# Jobs created by FARMOS itself for verification (redundant shadows) are not
# revenue work; they are exempt from adapter kill switches and profitability.
SYSTEM_ADAPTER_IDS = {"internal-verification", "system"}


def get(adapter_id: str) -> RevenueAdapter | None:
    return ADAPTERS.get(adapter_id)
