import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from app.api import jobs
from app.services import profitability as prof
from app.workloads import cpu_data


@pytest.mark.asyncio
async def test_pilot_queues_own_data_without_fake_price(monkeypatch):
    db = SimpleNamespace(add=MagicMock(), flush=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(jobs.opps, 'internal_opportunity', AsyncMock(return_value=None))
    monkeypatch.setattr(jobs.opps, 'adapter_enabled', AsyncMock(return_value=True))
    monkeypatch.setattr(jobs.opps, 'cost_policy', AsyncMock(return_value=prof.CostPolicy()))
    monkeypatch.setattr(jobs.econ, 'record_estimate', AsyncMock())
    monkeypatch.setattr(jobs.leases, 'add_event', MagicMock())
    req = jobs.JobCreateRequest(workload_type='json_transform', pilot_mode=True,
        expected_revenue_usd=999, auto_schedule=True,
        payload={'document':[{'units':2},{'units':3}], 'ops':[{'op':'sum','field':'units'}]})
    out = await jobs.create_job(req, db, SimpleNamespace(id=uuid4()), None)
    job = db.add.call_args.args[0]
    assert job.expected_revenue_usd == 0
    assert job.constraints == {'customer_price_usd':0.0, 'pilot_mode':True}
    assert out['status'] == 'VALIDATED'  # manual safety-checked dispatch only
    assert cpu_data.execute(job.workload_type, job.requested_resources)['output'] == 5


@pytest.mark.asyncio
async def test_pilot_rejects_network_work():
    with pytest.raises(HTTPException) as e:
        await jobs.create_job(jobs.JobCreateRequest(workload_type='http_check', pilot_mode=True),
                              None, SimpleNamespace(id=uuid4()), None)
    assert e.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['PENDING','FAILED','PASSED'])
async def test_verified_result_only_after_independent_pass(status):
    job = SimpleNamespace(id=uuid4(), constraints={'pilot_mode':True},
        verification=SimpleNamespace(status=status, method='reexecute', evidence={}, verified_at=None))
    result = SimpleNamespace(result_hash='a'*64, result_uri=json.dumps({'output':5}))
    first, second = MagicMock(), MagicMock()
    first.scalar_one_or_none.return_value = job
    second.scalar_one_or_none.return_value = result
    db = SimpleNamespace(execute=AsyncMock(side_effect=[first,second]))
    if status != 'PASSED':
        with pytest.raises(HTTPException) as e:
            await jobs.verified_result(job.id, db, SimpleNamespace(id=uuid4()))
        assert e.value.status_code == 409
        assert db.execute.await_count == 1
    else:
        out = await jobs.verified_result(job.id, db, SimpleNamespace(id=uuid4()))
        assert out['result']['output'] == 5
        assert out['confirmed_crypto_payment'] is None
        assert out['pilot_mode']


@pytest.mark.asyncio
async def test_verified_pilot_never_receives_test_earnings(monkeypatch):
    from app.services import verification_service as v, job_executor, economics_service
    from app.db.models.enums import JobStatus
    db_result = MagicMock()
    db_result.scalar_one_or_none.return_value = None
    db = SimpleNamespace(execute=AsyncMock(return_value=db_result), add=MagicMock(), flush=AsyncMock())
    job = SimpleNamespace(id=uuid4(), verification_spec={'role':'PRIMARY'}, constraints={'pilot_mode':True},
        verification=None, retry_count=0, status=JobStatus.RESULT_SUBMITTED, workload_type='json_transform')
    result = SimpleNamespace(device_id=uuid4(), result_hash='a'*64)
    credit = AsyncMock()
    monkeypatch.setattr(job_executor, 'credit_device_earnings', credit)
    monkeypatch.setattr(v.leases, 'latest_lease', AsyncMock(return_value=None))
    monkeypatch.setattr(v.leases, 'add_event', MagicMock())
    monkeypatch.setattr(economics_service, 'finalize_job', AsyncMock(return_value={'gross':0}))
    out = await v.finalize(db, job, result, v.Outcome(v.PASSED, 'DETERMINISTIC_RECOMPUTE'))
    assert out['job_status'] == 'VERIFIED'
    assert not out['test_earnings_credited']
    credit.assert_not_awaited()
