"""Phase 2: deterministic CPU/data workloads + cross-language vectors."""

import base64
import zlib

import pytest

from app.workloads import cpu_data

# Cross-language known-answer vectors. The SAME values are asserted in
# android-node CpuDataExecutorTest.kt — change both or neither.
VEC_DET_STREAM_ABC_8 = "cf2db1ac9867debd"
VEC_CHECKSUM_ABC_100 = "21d789823b7cbf0f789394a475d818cf277c3e074572ae3ab351628650e19fef"
VEC_DET_TEXT_ABC_30 = b"farmos-abc-0\nfarmos-abc-1\nfarm"


def test_cross_language_vectors():
    assert cpu_data.det_stream("abc", 8).hex() == VEC_DET_STREAM_ABC_8
    assert cpu_data.det_text("abc", 30) == VEC_DET_TEXT_ABC_30
    r = cpu_data.run_checksum({"seed": "abc", "size_bytes": 100})
    assert r["output_sha256"] == VEC_CHECKSUM_ABC_100
    assert r["status"] == "COMPLETED" and r["size_bytes"] == 100


def test_checksum_rounds_and_determinism():
    a = cpu_data.run_checksum({"seed": "s", "size_bytes": 4096, "rounds": 3})
    b = cpu_data.run_checksum({"seed": "s", "size_bytes": 4096, "rounds": 3})
    c = cpu_data.run_checksum({"seed": "s", "size_bytes": 4096, "rounds": 1})
    assert a == b and a["output_sha256"] != c["output_sha256"]


@pytest.mark.parametrize("payload", [
    {"size_bytes": 10}, {"seed": "x", "size_bytes": 0}, {"seed": "x", "size_bytes": 10**9},
    {"seed": "x", "size_bytes": 10, "rounds": 99}, {"seed": "x", "size_bytes": True},
])
def test_checksum_rejects_bad_payload(payload):
    out = cpu_data.execute("checksum", payload)
    assert out["status"] == "FAILED"
    with pytest.raises(cpu_data.WorkloadError):
        cpu_data.validate_payload("checksum", payload)


def test_compression_roundtrip_verification_independent_of_level():
    payload = {"seed": "zz", "size_bytes": 20000}
    for level in (1, 6, 9):
        r = cpu_data.run_compression(payload, level=level)
        assert cpu_data.verify_compression(payload, r)[0]
    assert r["compressed_size"] < r["input_size"] // 5


def test_compression_tamper_detected():
    payload = {"seed": "zz", "size_bytes": 5000}
    r = cpu_data.run_compression(payload)
    bad = dict(r, compressed_b64=base64.b64encode(zlib.compress(b"nope")).decode())
    assert not cpu_data.verify_compression(payload, bad)[0]
    bad2 = dict(r, input_sha256="0" * 64)
    assert not cpu_data.verify_compression(payload, bad2)[0]
    bad3 = dict(r, compressed_b64="!!!")
    assert not cpu_data.verify_compression(payload, bad3)[0]


def test_json_transform_ops():
    doc = [{"id": 1, "g": "a", "v": 5}, {"id": 2, "g": "b", "v": 7}, {"id": 3, "g": "a", "v": 9.0}]
    r = cpu_data.run_json_transform({"document": doc, "ops": [{"op": "filter_eq", "field": "g", "value": "a"},
                                                              {"op": "sum", "field": "v"}]})
    assert r["output"] == 14
    r2 = cpu_data.run_json_transform({"document": doc, "ops": [{"op": "select", "fields": ["id"]},
                                                               {"op": "count"}]})
    assert r2["output"] == 3
    with pytest.raises(cpu_data.WorkloadError):
        cpu_data.run_json_transform({"document": [{"v": 1.5}], "ops": [{"op": "sum", "field": "v"}]})
    assert cpu_data.execute("json_transform", {"document": [], "ops": [{"op": "bogus"}]})["status"] == "FAILED"


def test_canonical_normalizes_integral_floats():
    assert cpu_data.canonical({"b": 5.0, "a": [1.0, 2.5, True]}) == '{"a":[1,2.5,true],"b":5}'
