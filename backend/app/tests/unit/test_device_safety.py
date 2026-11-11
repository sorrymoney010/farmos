"""Unit tests: thermal/battery safety governor (spec §10, §60) — protect hardware."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.db.models.enums import DeviceStatus, TemperatureState
from app.services.device_safety import (
    ThermalThresholds,
    allowed_workload_class,
    dispatch_block_reason,
    evaluate_heartbeat,
    temperature_band,
)

T = ThermalThresholds()
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def ev(status=DeviceStatus.ACTIVE, temp=30.0, pct=80.0, charging=True, health="GOOD",
       cooldown_until=None, red=0, now=NOW):
    return evaluate_heartbeat(status=status, temperature_c=temp, battery_pct=pct, charging=charging,
                              battery_health=health, cooldown_until=cooldown_until,
                              recent_red_events=red, now=now, t=T)


@pytest.mark.parametrize("temp,band", [
    (None, TemperatureState.UNKNOWN), (37.9, TemperatureState.GREEN), (38.0, TemperatureState.YELLOW),
    (41.9, TemperatureState.YELLOW), (42.0, TemperatureState.ORANGE), (45.0, TemperatureState.ORANGE),
    (45.1, TemperatureState.RED),
])
def test_spec_thermal_bands(temp, band):
    assert temperature_band(temp, T) == band


def test_thresholds_must_be_ordered():
    with pytest.raises(ValueError):
        ThermalThresholds(green_max=45, yellow_max=42)


def test_green_all_workloads():
    d = ev()
    assert d.workload_class == "ALL" and not d.paused and d.new_status == DeviceStatus.ACTIVE


def test_yellow_throttles_concurrency():
    d = ev(temp=40)
    assert d.workload_class == "ALL" and d.max_concurrency == 1
    assert d.new_status == DeviceStatus.THROTTLED


def test_orange_light_only():
    d = ev(temp=43)
    assert d.workload_class == "LIGHT" and d.new_status == DeviceStatus.THROTTLED


def test_red_enters_cooling_with_cooldown():
    d = ev(status=DeviceStatus.RUNNING, temp=46)
    assert d.new_status == DeviceStatus.COOLING
    assert d.temperature_state == TemperatureState.COOLING
    assert d.paused and d.workload_class == "NONE" and d.red_event
    assert d.cooldown_until == NOW + timedelta(seconds=T.cooldown_seconds)


def test_still_hot_in_cooldown_extends_without_new_red_episode():
    d = ev(status=DeviceStatus.COOLING, temp=47, cooldown_until=NOW + timedelta(seconds=100), red=2)
    assert d.new_status == DeviceStatus.COOLING and not d.red_event and not d.quarantine


def test_cooling_does_not_resume_before_window_even_if_green():
    d = ev(status=DeviceStatus.COOLING, temp=30, cooldown_until=NOW + timedelta(seconds=60))
    assert d.new_status == DeviceStatus.COOLING and d.paused


def test_cooling_does_not_resume_unless_green():
    d = ev(status=DeviceStatus.COOLING, temp=40, cooldown_until=NOW - timedelta(seconds=1))
    assert d.new_status == DeviceStatus.COOLING and d.paused


def test_cooling_resumes_to_idle_after_window_and_green():
    d = ev(status=DeviceStatus.COOLING, temp=34, cooldown_until=NOW - timedelta(seconds=1))
    assert d.new_status == DeviceStatus.IDLE and not d.paused and d.cooldown_until is None


def test_emergency_stop():
    d = ev(temp=51)
    assert d.emergency_stop and d.workload_class == "NONE"


def test_repeated_red_quarantines():
    d = ev(temp=46, red=T.red_events_quarantine - 1)
    assert d.quarantine and d.new_status == DeviceStatus.QUARANTINED


def test_battery_fault_quarantines():
    d = ev(health="OVER_VOLTAGE")
    assert d.quarantine and d.new_status == DeviceStatus.QUARANTINED and d.emergency_stop


def test_battery_overheat_cools():
    d = ev(temp=33, health="OVERHEAT")
    assert d.new_status == DeviceStatus.COOLING


def test_quarantined_never_auto_released():
    d = ev(status=DeviceStatus.QUARANTINED, temp=25)
    assert d.new_status == DeviceStatus.QUARANTINED and d.workload_class == "NONE"


def test_low_battery_pauses_when_discharging():
    d = ev(pct=30, charging=False)
    assert d.paused and d.workload_class == "NONE"
    assert ev(pct=30, charging=True).workload_class == "ALL"
    assert ev(pct=10, charging=True).workload_class == "NONE"  # critical even on charger


def test_heavy_work_off_charger_needs_headroom_and_full_hot_charge_is_light():
    assert allowed_workload_class(TemperatureState.GREEN, 50, False, T) == "LIGHT"
    assert allowed_workload_class(TemperatureState.GREEN, 80, False, T) == "ALL"
    assert allowed_workload_class(TemperatureState.YELLOW, 100, True, T) == "LIGHT"
    assert allowed_workload_class(TemperatureState.UNKNOWN, 100, True, T) == "LIGHT"


def test_onboarding_status_preserved_during_red():
    d = ev(status=DeviceStatus.BENCHMARKING, temp=46)
    assert d.new_status == DeviceStatus.BENCHMARKING and d.cooldown_until is not None


def _dev(**kw):
    base = dict(status=DeviceStatus.ACTIVE, cooldown_until=None, safety_paused=False, safety_reason=None,
                temperature_state="GREEN", last_battery_pct=90, last_charging=True)
    base.update(kw)
    return SimpleNamespace(**base)


def test_dispatch_block_reason():
    assert dispatch_block_reason(_dev(), "benchmark", T, NOW) is None
    assert "QUARANTINED" in dispatch_block_reason(_dev(status=DeviceStatus.QUARANTINED), "http_check", T, NOW)
    assert "cooling" in dispatch_block_reason(_dev(cooldown_until=NOW + timedelta(seconds=5)), "http_check", T, NOW)
    assert "not allowed" in dispatch_block_reason(_dev(temperature_state="ORANGE"), "benchmark", T, NOW)
    assert dispatch_block_reason(_dev(temperature_state="ORANGE"), "http_check", T, NOW) is None
    assert dispatch_block_reason(_dev(temperature_state=None), "http_check", T, NOW) is None
    assert "benchmark must pass" in dispatch_block_reason(_dev(status=DeviceStatus.BENCHMARKING), "http_check", T, NOW)
    assert dispatch_block_reason(_dev(status=DeviceStatus.BENCHMARKING), "benchmark", T, NOW) is None
    assert "safety paused" in dispatch_block_reason(_dev(safety_paused=True, safety_reason="x"), "http_check", T, NOW)
