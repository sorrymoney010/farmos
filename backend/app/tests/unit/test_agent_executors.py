"""Phase 2: Python node agent executes CPU/data workloads that the server verifies."""

import asyncio
import uuid
from types import SimpleNamespace

from app.agent.executors import result_hash_and_uri, run_workload
from app.services import verification_service as v


def test_agent_checksum_result_verifies_server_side():
    payload = {"seed": "agent", "size_bytes": 50_000, "rounds": 2}
    result = asyncio.run(run_workload("checksum", payload))
    assert result["status"] == "COMPLETED" and isinstance(result["duration_ms"], int)
    h, uri = result_hash_and_uri(result)
    job = SimpleNamespace(id=uuid.uuid4(), workload_type="checksum", requested_resources=payload,
                          verification_spec={"role": "PRIMARY"})
    out = v.evaluate_static(job, SimpleNamespace(result_uri=uri, result_hash=h))
    assert out.status == v.PASSED, out.reason


def test_agent_compression_and_json_transform():
    for wt, p in [("compression", {"seed": "a", "size_bytes": 10_000}),
                  ("json_transform", {"document": [{"v": 2}, {"v": 3}], "ops": [{"op": "sum", "field": "v"}]})]:
        r = asyncio.run(run_workload(wt, p))
        h, uri = result_hash_and_uri(r)
        job = SimpleNamespace(id=uuid.uuid4(), workload_type=wt, requested_resources=p,
                              verification_spec={"role": "PRIMARY"})
        assert v.evaluate_static(job, SimpleNamespace(result_uri=uri, result_hash=h)).status == v.PASSED


def test_agent_unknown_workload_and_bad_url_fail_cleanly():
    assert asyncio.run(run_workload("nope", {}))["status"] == "FAILED"
    assert asyncio.run(run_workload("api_request", {"url": "ftp://x"}))["status"] == "FAILED"
