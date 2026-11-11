"""Unit tests for discovered-opportunity validation (Sprint 10 adapter hardening)."""

from app.adapters import guard
from app.adapters.base import OpportunitySpec


def _spec(**kw):
    base = dict(external_id="x1", name="n", category="c", workload_type="checksum",
                payload_template={"size_bytes": 1024})
    base.update(kw)
    return OpportunitySpec(**base)


def test_valid_spec_accepted():
    assert guard.validate_spec(_spec()) == []


def test_out_of_range_and_non_finite_rejected():
    errs = guard.validate_spec(_spec(thermal_intensity=2, risk_score=float("inf"), available_capacity=-1,
                                     expected_runtime_seconds=0, revenue_usd_per_job=True))
    joined = " ".join(errs)
    for name in ("thermal_intensity", "risk_score", "available_capacity", "expected_runtime_seconds",
                 "revenue_usd_per_job"):
        assert name in joined


def test_bad_payload_and_types_rejected():
    assert any("payload_template" in e for e in guard.validate_spec(_spec(payload_template={"size_bytes": -1})))
    assert guard.validate_spec({"external_id": "dict"}) == ["not an OpportunitySpec (dict)"]
    assert any("constraints" in e for e in guard.validate_spec(_spec(constraints=[1])))


def test_check_specs_dedupes_caps_and_handles_non_list():
    specs = [_spec(external_id=f"e{i}") for i in range(5)] + [_spec(external_id="e0")]
    res = guard.check_specs(specs, max_count=10)
    assert len(res.accepted) == 5 and res.rejected[0]["errors"] == ["duplicate external_id"]
    capped = guard.check_specs(specs, max_count=3)
    assert len(capped.accepted) == 3 and capped.truncated == 3
    assert guard.check_specs("nope").accepted == []
