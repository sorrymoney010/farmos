"""Phone readiness observations, never an assertion of network eligibility or income."""
import math
from datetime import datetime, timezone


def processor_readiness(processor, *, now=None, stale_seconds=3600):
    now = now or datetime.now(timezone.utc)
    seen = processor.last_reported_at
    if seen is not None and seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    age = (now - seen).total_seconds() if seen is not None else None
    blockers = []
    if not processor.enabled:
        blockers.append('disabled')
    if age is None:
        blockers.append('awaiting_signed_check_in')
    elif age < -60:
        blockers.append('invalid_future_timestamp')
    elif age > stale_seconds:
        blockers.append('stale_check_in')
    def valid(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    temperature = processor.temperature_c
    battery = processor.battery_level
    if not valid(temperature) or not -10 <= temperature <= 80:
        blockers.append('temperature_unknown')
    elif temperature >= 42:
        blockers.append('cooling_required')
    if not valid(battery) or not 0 <= battery <= 100:
        blockers.append('battery_unknown')
    elif battery < 20:
        blockers.append('low_battery')
    if processor.is_charging is not True:
        blockers.append('connect_power')
    if processor.network_type not in ('wifi', 'usb'):
        blockers.append('preferred_network_required')
    return {
        'processor_id': str(processor.id), 'label': processor.label,
        'address': processor.address, 'source': 'acurast_management_bridge',
        'farmos_device_id': str(processor.farmos_device_id) if processor.farmos_device_id else None,
        'enabled': processor.enabled, 'telemetry_ready': not blockers, 'blockers': blockers,
        'last_check_in': seen.isoformat() if seen else None,
        'check_in_age_seconds': max(0, int(age)) if age is not None else None,
        'temperature_c': temperature if valid(temperature) else None,
        'battery_pct': battery if valid(battery) else None,
        'charging': processor.is_charging, 'network_type': processor.network_type,
        'network_eligibility': 'UNVERIFIED', 'reward_verification': 'NOT_CONNECTED',
        'confirmed_rewards': None, 'earning_status': 'UNVERIFIED',
    }
