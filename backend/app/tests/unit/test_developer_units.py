"""Sprint 16 unit tests: Stripe signature scheme, form encoding, mode gating,
URL policy, schedule validation, API key parsing. No network."""

import json
import time

import pytest

from app.config import settings
from app.services.developer import accounts, deployments, stripe_client
from app.services.finance import ledger

SECRET = "whsec_unit_only"


def test_signature_roundtrip_tolerance_and_multiple_v1():
    raw = json.dumps({"id": "evt_1", "type": "x"}).encode()
    hdr = stripe_client.sign_payload(raw, SECRET, 1_700_000_000)
    assert stripe_client.verify_and_parse(raw, hdr, SECRET, 300, now=1_700_000_100)["id"] == "evt_1"
    with pytest.raises(stripe_client.SignatureError):
        stripe_client.verify_and_parse(raw, hdr, SECRET, 300, now=1_700_000_400)
    with pytest.raises(stripe_client.SignatureError):
        stripe_client.verify_and_parse(raw + b" ", hdr, SECRET, 300, now=1_700_000_000)
    rotated = hdr + ",v1=" + "0" * 64          # Stripe sends several v1 during secret rotation
    assert stripe_client.verify_and_parse(raw, rotated, SECRET, 300, now=1_700_000_000)
    with pytest.raises(stripe_client.SignatureError):
        stripe_client.verify_and_parse(raw, hdr, "", 300)
    with pytest.raises(stripe_client.SignatureError):
        stripe_client.verify_and_parse(b"[]", stripe_client.sign_payload(b"[]", SECRET), SECRET)


def test_flatten_matches_stripe_bracket_encoding():
    flat = dict(stripe_client._flatten({"mode": "payment", "line_items": [{"price_data": {"unit_amount": 500}}],
                                        "metadata": {"a": "b"}, "skip": None, "flag": True}))
    assert flat == {"mode": "payment", "line_items[0][price_data][unit_amount]": "500", "metadata[a]": "b",
                    "flag": "true"}


def test_mode_gating(monkeypatch):
    for key, env, enabled, expect in [("", "production", True, "unconfigured"),
                                      ("sk_test_x", "development", False, "test"),
                                      ("rk_test_x", "test", False, "test"),
                                      ("sk_live_x", "development", True, "live_disabled"),
                                      ("sk_live_x", "production", False, "live_disabled"),
                                      ("sk_live_x", "production", True, "live"),
                                      ("pk_test_x", "test", False, "unconfigured")]:
        monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", key)
        monkeypatch.setattr(settings, "APP_ENV", env)
        monkeypatch.setattr(settings, "STRIPE_LIVE_MODE_ENABLED", enabled)
        assert stripe_client.mode() == expect, (key, env, enabled)
        assert stripe_client.book() == (ledger.REAL if expect == "live" else ledger.TEST)


@pytest.mark.parametrize("url", ["https://example.com", "https://api.example.co.uk/v1/health?x=1",
                                 "https://example.com:8443/", "https://8.8.8.8/"])
def test_url_policy_allows_public_https(url):
    assert deployments.check_url(url) == url


@pytest.mark.parametrize("url", ["http://example.com", "ftp://example.com", "https://localhost", "https://intranet",
                                 "https://127.0.0.1", "https://10.1.1.1", "https://172.16.0.1", "https://192.168.0.1",
                                 "https://169.254.169.254", "https://[::1]", "https://[fe80::1]", "https://0.0.0.0",
                                 "https://nas.local", "https://svc.internal", "https://a:b@example.com",
                                 "https://example.com:6379", None, "x" * 3000])
def test_url_policy_blocks_private_and_odd_targets(url):
    with pytest.raises(deployments.DeploymentError):
        deployments.check_url(url)


def test_schedule_validation():
    assert deployments.normalize_schedule({}) == {"max_runs": 1}
    assert deployments.normalize_schedule({"interval_seconds": 60, "max_runs": 5}) == {"interval_seconds": 60,
                                                                                       "max_runs": 5}
    for bad in ({"interval_seconds": 59, "max_runs": 2}, {"interval_seconds": 60}, {"max_runs": 3},
                {"interval_seconds": 60, "max_runs": 0}, {"cron": "* * * * *"},
                {"interval_seconds": 60, "end_at": "2000-01-01T00:00:00Z"}):
        with pytest.raises(deployments.DeploymentError):
            deployments.normalize_schedule(bad)
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 86400))
    assert "end_at" in deployments.normalize_schedule({"interval_seconds": 300, "end_at": end})


def test_api_key_parsing_and_hashing():
    assert accounts.parse_key("fos_test_0123456789ab_secret") == ("test", "0123456789ab")
    for bad in ("fos_prod_0123456789ab_s", "fos_test_short_s", "sk_test_x", "fos_test_0123456789ab", ""):
        assert accounts.parse_key(bad) is None
    h = accounts.hash_key("fos_test_0123456789ab_secret")
    assert len(h) == 64 and h != accounts.hash_key("fos_test_0123456789ab_secreT")
    with pytest.raises(ValueError):
        accounts.validate_scopes(["jobs:read", "root"])
    assert accounts.validate_scopes(None) == sorted(accounts.DEFAULT_SCOPES)
