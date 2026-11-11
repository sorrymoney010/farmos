"""Thermal + hardware safety governor (spec §10, §60, §62, §63, §68).

Principle (non-negotiable): *Do not make $5 while destroying a $50 device.*

This module is pure (no I/O) so the policy is easy to test and audit. The
heartbeat endpoint feeds telemetry in, persists the decision, and the job
dispatch/poll/accept paths call :func:`dispatch_block_reason` before handing any
work to a node. The Android node enforces the same thresholds locally (defense
in depth), so a dead control plane can never keep a hot phone working.

Thermal bands (configurable, per-model overrides supported):
    GREEN  : t <  green_max (38 C)           normal operation
    YELLOW : green_max <= t < yellow_max (42) reduce concurrency (max 1 job)
    ORANGE : yellow_max <= t <= orange_max (45) light workloads only
    RED    : t > orange_max                    stop compute, cool down
    EMERGENCY: t >= emergency (50)             emergency stop everything
    UNKNOWN: no temperature reported           light workloads only

After RED a device enters COOLING for a per-device cooldown window and only
resumes (-> IDLE) once the window has passed AND it is back in GREEN.
Repeated RED events in a window, or a battery hardware fault, quarantine the
device; quarantine is only ever released explicitly by an admin (spec §57.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.db.models.enums import DeviceStatus, JobType, TemperatureState

LIGHT_WORKLOADS = {
    JobType.HTTP_CHECK.value,
    JobType.MONITOR.value,
    JobType.API_REQUEST.value,
}
HEAVY_WORKLOADS = {JobType.DATA_TRANSFORM.value, JobType.BENCHMARK.value}

# Android BatteryManager health strings that indicate a hardware fault.
BATTERY_FAULTS = {"DEAD", "OVER_VOLTAGE", "UNSPECIFIED_FAILURE"}

# Statuses that safety automation must never change (admin-only exits).
LOCKED_STATUSES = {DeviceStatus.QUARANTINED, DeviceStatus.SUSPENDED, DeviceStatus.RETIRED}
# Statuses whose lifecycle is owned by enrollment/benchmark, not by safety.
ONBOARDING_STATUSES = {DeviceStatus.ENROLLING, DeviceStatus.BENCHMARKING}
SCHEDULABLE_STATUSES = {DeviceStatus.ACTIVE, DeviceStatus.IDLE, DeviceStatus.THROTTLED}


@dataclass(frozen=True)
class ThermalThresholds:
    green_max: float = 38.0
    yellow_max: float = 42.0
    orange_max: float = 45.0
    emergency: float = 50.0
    cooldown_seconds: int = 600
    battery_min_pct_discharging: float = 40.0
    battery_critical_pct: float = 15.0
    battery_heavy_min_pct_discharging: float = 60.0
    red_events_quarantine: int = 3
    red_window_seconds: int = 3600

    def __post_init__(self) -> None:
        if not (self.green_max < self.yellow_max <= self.orange_max < self.emergency):
            raise ValueError("thermal thresholds must satisfy green < yellow <= orange < emergency")

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def thresholds_from_settings(settings: Any, model: str | None = None,
                             policy_override: dict[str, Any] | None = None) -> ThermalThresholds:
    """Build thresholds from app settings, an optional admin policy, and per-model overrides."""
    base: dict[str, Any] = {
        "green_max": settings.THERMAL_GREEN_MAX_C,
        "yellow_max": settings.THERMAL_YELLOW_MAX_C,
        "orange_max": settings.THERMAL_ORANGE_MAX_C,
        "emergency": settings.THERMAL_EMERGENCY_C,
        "cooldown_seconds": settings.THERMAL_COOLDOWN_SECONDS,
        "battery_min_pct_discharging": settings.BATTERY_MIN_PCT_DISCHARGING,
        "battery_critical_pct": settings.BATTERY_CRITICAL_PCT,
        "battery_heavy_min_pct_discharging": settings.BATTERY_HEAVY_MIN_PCT_DISCHARGING,
        "red_events_quarantine": settings.THERMAL_RED_EVENTS_QUARANTINE,
        "red_window_seconds": settings.THERMAL_RED_WINDOW_SECONDS,
    }
    models: dict[str, Any] = dict(settings.THERMAL_MODEL_OVERRIDES or {})
    if policy_override:
        base.update({k: v for k, v in (policy_override.get("default") or {}).items() if k in base})
        models.update(policy_override.get("models") or {})
    if model and model in models:
        base.update({k: v for k, v in models[model].items() if k in base})
    return ThermalThresholds(**base)


def temperature_band(temp_c: float | None, t: ThermalThresholds) -> TemperatureState:
    if temp_c is None:
        return TemperatureState.UNKNOWN
    if temp_c < t.green_max:
        return TemperatureState.GREEN
    if temp_c < t.yellow_max:
        return TemperatureState.YELLOW
    if temp_c <= t.orange_max:
        return TemperatureState.ORANGE
    return TemperatureState.RED


def allowed_workload_class(
    band: TemperatureState,
    battery_pct: float | None,
    charging: bool | None,
    t: ThermalThresholds,
) -> str:
    """Return "ALL", "LIGHT" or "NONE" for the given health snapshot."""
    if band in (TemperatureState.RED, TemperatureState.COOLING):
        return "NONE"
    if battery_pct is not None:
        if battery_pct < t.battery_critical_pct:
            return "NONE"
        if not charging and battery_pct < t.battery_min_pct_discharging:
            return "NONE"
    if band in (TemperatureState.ORANGE, TemperatureState.UNKNOWN):
        return "LIGHT"
    # Avoid repeated full-charge + high-temperature cycles (spec §10).
    if charging and battery_pct is not None and battery_pct >= 99 and band != TemperatureState.GREEN:
        return "LIGHT"
    # Heavy work off-charger needs extra battery headroom.
    if not charging and (battery_pct is None or battery_pct < t.battery_heavy_min_pct_discharging):
        return "LIGHT"
    return "ALL"


@dataclass
class SafetyDecision:
    temperature_state: TemperatureState
    workload_class: str  # ALL | LIGHT | NONE
    max_concurrency: int
    paused: bool
    reason: str | None
    new_status: DeviceStatus
    cooldown_until: datetime | None
    red_event: bool = False
    quarantine: bool = False
    emergency_stop: bool = False
    events: list[tuple[str, str, str]] = field(default_factory=list)  # (type, severity, msg)

    def to_response(self) -> dict[str, Any]:
        return {
            "temperature_state": self.temperature_state.value,
            "workload_class": self.workload_class,
            "max_concurrency": self.max_concurrency,
            "paused": self.paused,
            "reason": self.reason,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "emergency_stop": self.emergency_stop,
        }


def evaluate_heartbeat(
    *,
    status: DeviceStatus,
    temperature_c: float | None,
    battery_pct: float | None,
    charging: bool | None,
    battery_health: str | None,
    cooldown_until: datetime | None,
    recent_red_events: int,
    now: datetime,
    t: ThermalThresholds,
) -> SafetyDecision:
    """Decide the device's safety state from one heartbeat's telemetry."""
    health = (battery_health or "").upper() or None
    band = temperature_band(temperature_c, t)

    def keep_or(target: DeviceStatus) -> DeviceStatus:
        if status in LOCKED_STATUSES or status in ONBOARDING_STATUSES:
            return status
        return target

    # 1. Locked statuses: never auto-release, never schedule.
    if status in LOCKED_STATUSES:
        return SafetyDecision(band, "NONE", 0, True, f"device {status.value}", status, cooldown_until)

    # 2. Battery hardware fault -> quarantine (swelling / fire risk).
    if health in BATTERY_FAULTS:
        return SafetyDecision(
            band, "NONE", 0, True, f"battery fault: {health}", DeviceStatus.QUARANTINED,
            cooldown_until, quarantine=True, emergency_stop=True,
            events=[("BATTERY_FAULT", "CRITICAL", f"Battery health {health}; device quarantined")],
        )

    # 3. RED / battery overheat / emergency -> stop compute, start (or extend) cooldown.
    emergency = temperature_c is not None and temperature_c >= t.emergency
    if band == TemperatureState.RED or health == "OVERHEAT" or emergency:
        until = now + timedelta(seconds=t.cooldown_seconds)
        why = (
            f"EMERGENCY {temperature_c:.1f}C >= {t.emergency:.0f}C" if emergency
            else f"thermal RED {temperature_c:.1f}C > {t.orange_max:.0f}C" if band == TemperatureState.RED
            else "battery OVERHEAT"
        )
        already_cooling = status == DeviceStatus.COOLING or (
            cooldown_until is not None and now < cooldown_until
        )
        if already_cooling:
            # Still hot inside an existing cooldown: extend the window, but do not
            # count another RED episode (one episode per entry into cooldown).
            return SafetyDecision(
                TemperatureState.COOLING, "NONE", 0, True, why, keep_or(DeviceStatus.COOLING),
                until, emergency_stop=emergency,
            )
        quarantine = recent_red_events + 1 >= t.red_events_quarantine
        events = [("THERMAL_RED", "CRITICAL", f"{why}; cooling until {until.isoformat()}")]
        if quarantine:
            events.append(("THERMAL_INSTABILITY", "CRITICAL",
                           f"{recent_red_events + 1} RED events within {t.red_window_seconds}s; quarantined"))
        return SafetyDecision(
            TemperatureState.COOLING, "NONE", 0, True, why,
            DeviceStatus.QUARANTINED if quarantine else keep_or(DeviceStatus.COOLING),
            until, red_event=True, quarantine=quarantine, emergency_stop=emergency or quarantine,
            events=events,
        )

    # 4. Inside a cooldown window: resume only when window passed AND back to GREEN.
    in_cooldown = status == DeviceStatus.COOLING or (cooldown_until is not None and now < cooldown_until)
    if in_cooldown:
        if (cooldown_until is None or now >= cooldown_until) and band == TemperatureState.GREEN:
            resumed_status = keep_or(DeviceStatus.IDLE)
            cls = allowed_workload_class(band, battery_pct, charging, t)
            return SafetyDecision(
                band, cls, 0 if cls == "NONE" else 2, cls == "NONE",
                None if cls != "NONE" else "battery policy", resumed_status, None,
                events=[("THERMAL_RECOVERED", "INFO", f"Cooled to {temperature_c}C; resumed")],
            )
        return SafetyDecision(
            TemperatureState.COOLING, "NONE", 0, True,
            "cooling down (needs GREEN after cooldown window)", keep_or(DeviceStatus.COOLING),
            cooldown_until,
        )

    # 5. Normal / throttled operation.
    cls = allowed_workload_class(band, battery_pct, charging, t)
    reason = None
    if cls == "NONE":
        reason = (
            f"battery critical {battery_pct}%" if battery_pct is not None and battery_pct < t.battery_critical_pct
            else f"battery {battery_pct}% < {t.battery_min_pct_discharging:.0f}% while not charging"
        )
    elif band == TemperatureState.YELLOW:
        reason = "thermal YELLOW: reduced concurrency"
    elif band == TemperatureState.ORANGE:
        reason = "thermal ORANGE: light workloads only"
    elif band == TemperatureState.UNKNOWN:
        reason = "no temperature telemetry: light workloads only"

    if band in (TemperatureState.YELLOW, TemperatureState.ORANGE) and status in {
        DeviceStatus.ACTIVE, DeviceStatus.IDLE, DeviceStatus.RUNNING
    }:
        new_status = DeviceStatus.THROTTLED
    elif band == TemperatureState.GREEN and status == DeviceStatus.THROTTLED:
        new_status = DeviceStatus.IDLE
    else:
        new_status = status

    max_conc = {"NONE": 0, "LIGHT": 1}.get(cls, 1 if band == TemperatureState.YELLOW else 2)
    return SafetyDecision(band, cls, max_conc, cls == "NONE", reason, new_status, None)


def workload_allowed(workload_type: str, workload_class: str) -> bool:
    if workload_class == "ALL":
        return True
    if workload_class == "LIGHT":
        return workload_type in LIGHT_WORKLOADS
    return False


def dispatch_block_reason(device: Any, workload_type: str, t: ThermalThresholds, now: datetime) -> str | None:
    """Return why ``device`` must NOT receive ``workload_type`` now, or None if allowed.

    Works off the persisted health snapshot on the Device row (written by the
    heartbeat path). Every job dispatch/poll/accept must pass this check
    (coder rule §53.9: no device job without policy check).
    """
    status = device.status
    if status in LOCKED_STATUSES:
        return f"device is {status.value}"
    if status in ONBOARDING_STATUSES:
        if not (status == DeviceStatus.BENCHMARKING and workload_type == JobType.BENCHMARK.value):
            return f"device is {status.value}; benchmark must pass first"
    elif status not in SCHEDULABLE_STATUSES | {DeviceStatus.RUNNING}:
        return f"device not schedulable (status={status.value})"
    if device.cooldown_until is not None and now < device.cooldown_until:
        return f"device cooling down until {device.cooldown_until.isoformat()}"
    if device.safety_paused:
        return f"safety paused: {device.safety_reason or 'policy'}"
    band = TemperatureState(device.temperature_state) if device.temperature_state else TemperatureState.UNKNOWN
    cls = allowed_workload_class(
        band,
        float(device.last_battery_pct) if device.last_battery_pct is not None else None,
        device.last_charging,
        t,
    )
    if not workload_allowed(workload_type, cls):
        return f"workload {workload_type} not allowed in {band.value} (class={cls})"
    return None
