"""BenchmarkService + safety policy persistence (spec §57.2, §10, §76).

DB-facing glue around the pure :mod:`farm_score` and :mod:`device_safety`
modules: loads admin-tunable weights/policy from ``system_settings``, records
benchmark results, and applies heartbeat safety decisions to the device row.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.core import SystemSettings
from app.db.models.devices import Device, DeviceBenchmark, DeviceHealthEvent, DeviceHeartbeat
from app.db.models.enums import DeviceStatus
from app.logging import get_logger
from app.services import farm_score as fs
from app.services.device_safety import (
    SafetyDecision,
    ThermalThresholds,
    evaluate_heartbeat,
    thresholds_from_settings,
)

logger = get_logger(__name__)

WEIGHTS_KEY = "farmscore.weights"
SAFETY_POLICY_KEY = "thermal.policy"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── admin-tunable config ─────────────────────────────────────────────────────

async def _get_setting(db: AsyncSession, key: str) -> Any | None:
    row = (await db.execute(select(SystemSettings).where(SystemSettings.key == key))).scalar_one_or_none()
    return row.value if row else None


async def _put_setting(db: AsyncSession, key: str, value: Any, user_id: UUID | None, description: str) -> None:
    row = (await db.execute(select(SystemSettings).where(SystemSettings.key == key))).scalar_one_or_none()
    if row is None:
        db.add(SystemSettings(key=key, value=value, description=description, updated_by=user_id))
    else:
        row.value = value
        row.updated_by = user_id


async def get_weights(db: AsyncSession) -> dict[str, float]:
    stored = await _get_setting(db, WEIGHTS_KEY)
    return fs.normalise_weights(stored or settings.FARMSCORE_WEIGHTS)


async def set_weights(db: AsyncSession, weights: dict[str, float], user_id: UUID | None) -> dict[str, float]:
    normalised = fs.normalise_weights(weights)  # validates
    await _put_setting(db, WEIGHTS_KEY, normalised, user_id, "FarmScore component weights (spec §9)")
    return normalised


async def get_safety_policy(db: AsyncSession) -> dict[str, Any] | None:
    return await _get_setting(db, SAFETY_POLICY_KEY)


async def set_safety_policy(db: AsyncSession, policy: dict[str, Any], user_id: UUID | None) -> ThermalThresholds:
    thresholds_from_settings(settings, None, policy)  # validates default
    for model in (policy.get("models") or {}):
        thresholds_from_settings(settings, model, policy)  # validates every override
    await _put_setting(db, SAFETY_POLICY_KEY, policy, user_id, "Thermal/battery safety policy (spec §10)")
    return thresholds_from_settings(settings, None, policy)


async def thresholds_for_device(db: AsyncSession, device: Device) -> ThermalThresholds:
    return thresholds_from_settings(settings, device.model, await get_safety_policy(db))


# ── benchmarks ───────────────────────────────────────────────────────────────

def results_sha256(raw_results_json: str) -> str:
    return hashlib.sha256(raw_results_json.encode("utf-8")).hexdigest()


async def device_reliability(db: AsyncSession, device: Device, now: datetime | None = None) -> float:
    now = now or utcnow()
    since = now - timedelta(hours=24)
    count = (
        await db.execute(
            select(func.count(DeviceHeartbeat.id)).where(
                DeviceHeartbeat.device_id == device.id, DeviceHeartbeat.observed_at >= since
            )
        )
    ).scalar_one()
    enrolled = device.enrolled_at or device.created_at or now
    return fs.reliability_from_heartbeats(int(count), (now - enrolled).total_seconds())


async def record_result(
    db: AsyncSession,
    device: Device,
    suite_version: str,
    raw: dict[str, Any],
    *,
    job_id: UUID | None = None,
) -> tuple[DeviceBenchmark, bool, list[str]]:
    """Score raw results, persist a DeviceBenchmark, update device.farm_score.

    Returns (benchmark, passed_baseline, reasons_if_failed).
    """
    thresholds = await thresholds_for_device(db, device)
    reliability = await device_reliability(db, device)
    comps = fs.score_components(raw, green_max_c=thresholds.green_max, reliability=reliability)
    weights = await get_weights(db)
    score = fs.calculate_farm_score(comps.as_dict(), weights)

    previous = (
        await db.execute(
            select(DeviceBenchmark)
            .where(DeviceBenchmark.device_id == device.id)
            .order_by(DeviceBenchmark.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    anomalies = list(comps.anomalies)
    if previous is not None and previous.farm_score is not None:
        jump = abs(score - float(previous.farm_score))
        if jump > 40:
            anomalies.append(f"farm_score jumped {jump:.1f} points vs previous benchmark")

    reasons: list[str] = []
    if anomalies:
        reasons.append("benchmark anomalies detected")
    if score < settings.BENCHMARK_MIN_FARM_SCORE:
        reasons.append(f"farm_score {score:.1f} < minimum {settings.BENCHMARK_MIN_FARM_SCORE}")
    peak = (raw.get("thermal") or {}).get("peak_c")
    if isinstance(peak, (int, float)) and peak > thresholds.orange_max:
        reasons.append(f"benchmark peak temperature {peak}C exceeded {thresholds.orange_max}C")
    health = str((raw.get("battery") or {}).get("health") or "").upper()
    if health in {"DEAD", "OVER_VOLTAGE", "UNSPECIFIED_FAILURE", "OVERHEAT"}:
        reasons.append(f"battery health {health}")
    passed = not reasons

    stored_raw = {
        "suite_version": suite_version,
        "measurements": raw,
        "weights": weights,
        "component_scores": comps.as_dict(),
        "anomalies": anomalies,
        "passed_baseline": passed,
        "baseline_failures": reasons,
        "job_id": str(job_id) if job_id else None,
        "scorer": fs.SUITE_VERSION,
    }
    bench = DeviceBenchmark(
        device_id=device.id,
        suite_version=suite_version,
        cpu_score=comps.cpu,
        ai_score=comps.ai,
        memory_score=comps.memory,
        storage_score=comps.storage,
        network_score=comps.network,
        thermal_score=comps.thermal,
        power_efficiency_score=comps.power_efficiency,
        reliability_score=comps.reliability,
        farm_score=score,
        raw_results=stored_raw,
        completed_at=utcnow(),
    )
    db.add(bench)
    device.farm_score = score

    if anomalies:
        db.add(DeviceHealthEvent(
            device_id=device.id, event_type="BENCHMARK_ANOMALY", severity="WARNING",
            message="; ".join(anomalies)[:2000], event_metadata={"suite_version": suite_version},
        ))
    if passed and device.status == DeviceStatus.BENCHMARKING:
        device.status = DeviceStatus.ACTIVE
        db.add(DeviceHealthEvent(
            device_id=device.id, event_type="BASELINE_PASSED", severity="INFO",
            message=f"Benchmark passed (FarmScore {score:.1f}); device ACTIVE",
            event_metadata={"suite_version": suite_version},
        ))
    elif not passed:
        db.add(DeviceHealthEvent(
            device_id=device.id, event_type="BASELINE_FAILED", severity="WARNING",
            message="; ".join(reasons)[:2000], event_metadata={"suite_version": suite_version},
        ))
    await db.flush()
    return bench, passed, reasons


def benchmark_to_dict(b: DeviceBenchmark) -> dict[str, Any]:
    f = lambda v: float(v) if v is not None else None  # noqa: E731
    raw = b.raw_results or {}
    return {
        "benchmark_id": str(b.id),
        "suite_version": b.suite_version,
        "completed_at": b.completed_at.isoformat() if b.completed_at else None,
        "farm_score": f(b.farm_score),
        "scores": {
            "cpu": f(b.cpu_score),
            "ai": f(b.ai_score),
            "memory": f(b.memory_score),
            "storage": f(b.storage_score),
            "network": f(b.network_score),
            "thermal": f(b.thermal_score),
            "power_efficiency": f(b.power_efficiency_score),
            "reliability": f(b.reliability_score),
        },
        "weights": raw.get("weights"),
        "passed_baseline": raw.get("passed_baseline"),
        "baseline_failures": raw.get("baseline_failures", []),
        "anomalies": raw.get("anomalies", []),
    }


async def get_latest_scores(db: AsyncSession, device_id: UUID) -> DeviceBenchmark | None:
    return (
        await db.execute(
            select(DeviceBenchmark)
            .where(DeviceBenchmark.device_id == device_id)
            .order_by(DeviceBenchmark.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def recalculate_all_farm_scores(db: AsyncSession) -> int:
    """Re-apply current weights to each device's latest component scores."""
    weights = await get_weights(db)
    devices = (await db.execute(select(Device))).scalars().all()
    n = 0
    for d in devices:
        latest = await get_latest_scores(db, d.id)
        if latest is None:
            continue
        d.farm_score = fs.calculate_farm_score(benchmark_to_dict(latest)["scores"], weights)
        n += 1
    return n


# ── heartbeat safety ─────────────────────────────────────────────────────────

def effective_temperature(
    temperature_c: float | None,
    battery_temperature_c: float | None,
    thermal_status: int | None,
    t: ThermalThresholds,
) -> float | None:
    """Hottest reported sensor wins; Android PowerManager thermal status sets a floor.

    thermal_status (PowerManager.THERMAL_STATUS_*): 2=MODERATE -> at least ORANGE,
    >=3 SEVERE/CRITICAL/EMERGENCY/SHUTDOWN -> RED.
    """
    temps = [v for v in (temperature_c, battery_temperature_c) if v is not None]
    eff = max(temps) if temps else None
    if thermal_status is not None:
        if thermal_status >= 3:
            eff = max(eff or 0.0, t.orange_max + 0.1)
        elif thermal_status == 2:
            eff = max(eff or 0.0, t.yellow_max)
    return eff


async def apply_heartbeat_safety(
    db: AsyncSession,
    device: Device,
    *,
    temperature_c: float | None,
    battery_pct: float | None,
    charging: bool | None,
    battery_health: str | None,
    battery_temperature_c: float | None = None,
    thermal_status: int | None = None,
    now: datetime | None = None,
) -> SafetyDecision:
    now = now or utcnow()
    thresholds = await thresholds_for_device(db, device)
    temperature_c = effective_temperature(temperature_c, battery_temperature_c, thermal_status, thresholds)
    window_start = now - timedelta(seconds=thresholds.red_window_seconds)
    recent_red = (
        await db.execute(
            select(func.count(DeviceHealthEvent.id)).where(
                DeviceHealthEvent.device_id == device.id,
                DeviceHealthEvent.event_type == "THERMAL_RED",
                DeviceHealthEvent.created_at >= window_start,
            )
        )
    ).scalar_one()

    decision = evaluate_heartbeat(
        status=device.status,
        temperature_c=temperature_c,
        battery_pct=battery_pct,
        charging=charging,
        battery_health=battery_health,
        cooldown_until=device.cooldown_until,
        recent_red_events=int(recent_red),
        now=now,
        t=thresholds,
    )

    device.temperature_state = decision.temperature_state.value
    device.safety_paused = decision.paused
    device.safety_reason = decision.reason
    device.cooldown_until = decision.cooldown_until
    device.last_temperature_c = temperature_c
    device.last_battery_pct = battery_pct
    device.last_charging = charging
    device.battery_health = (battery_health or None)
    if decision.new_status != device.status:
        logger.info("device_safety_transition", device_id=str(device.id),
                    from_status=device.status.value, to_status=decision.new_status.value,
                    reason=decision.reason)
        device.status = decision.new_status

    for etype, severity, msg in decision.events:
        db.add(DeviceHealthEvent(
            device_id=device.id, event_type=etype, severity=severity, message=msg,
            event_metadata={"temperature_c": temperature_c, "battery_pct": battery_pct,
                            "charging": charging, "battery_health": battery_health},
        ))
    if decision.quarantine:
        db.add(DeviceHealthEvent(
            device_id=device.id, event_type="QUARANTINE", severity="CRITICAL",
            message=f"Auto-quarantine: {decision.reason}", event_metadata={"actor": "SAFETY_POLICY"},
        ))
    return decision


def parse_raw_results(raw_results_json: str) -> dict[str, Any]:
    data = json.loads(raw_results_json)
    if not isinstance(data, dict):
        raise ValueError("raw_results_json must encode a JSON object")
    return data
