from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.services.crypto_fleet import processor_readiness

NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)

def phone(**kw):
    data = dict(id=uuid4(), label='Phone', address='public-address', farmos_device_id=None,
                enabled=True, last_reported_at=NOW, temperature_c=30, battery_level=80,
                is_charging=True, network_type='wifi')
    return SimpleNamespace(**(data | kw))

def test_ready_is_not_earning():
    r = processor_readiness(phone(), now=NOW)
    assert r['telemetry_ready']
    assert r['confirmed_rewards'] is None
    assert r['earning_status'] == 'UNVERIFIED'
    assert r['network_eligibility'] == 'UNVERIFIED'
    assert 'ssid' not in r

@pytest.mark.parametrize('fields,blocker', [
    ({'enabled': False}, 'disabled'),
    ({'last_reported_at': None}, 'awaiting_signed_check_in'),
    ({'last_reported_at': NOW - timedelta(hours=2)}, 'stale_check_in'),
    ({'last_reported_at': NOW + timedelta(minutes=10)}, 'invalid_future_timestamp'),
    ({'temperature_c': 42}, 'cooling_required'),
    ({'temperature_c': None}, 'temperature_unknown'),
    ({'temperature_c': float('nan')}, 'temperature_unknown'),
    ({'battery_level': float('inf')}, 'battery_unknown'),
    ({'battery_level': 19}, 'low_battery'),
    ({'battery_level': -1}, 'battery_unknown'),
    ({'is_charging': False}, 'connect_power'),
    ({'network_type': 'cellular'}, 'preferred_network_required'),
])
def test_blockers(fields, blocker):
    r = processor_readiness(phone(**fields), now=NOW)
    assert not r['telemetry_ready']
    assert blocker in r['blockers']
    assert r['confirmed_rewards'] is None

def test_naive_database_timestamp_and_boundary():
    assert processor_readiness(phone(last_reported_at=NOW.replace(tzinfo=None)), now=NOW)['telemetry_ready']
    assert processor_readiness(phone(last_reported_at=NOW - timedelta(seconds=3600)), now=NOW)['telemetry_ready']


def test_fleet_route_requires_authentication():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.acurast import admin_router
    app = FastAPI()
    app.include_router(admin_router, prefix='/acurast')
    with TestClient(app) as client:
        assert client.get('/acurast/fleet-readiness').status_code == 401
