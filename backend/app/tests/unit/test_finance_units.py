"""Phase 4 unit tests: ledger validation, revenue split, withdrawal risk,
mock signer rules, SIWE / EIP-191 recovery. Ephemeral keys are generated
in-process for tests only and never persisted."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from app.payouts import get_provider, get_signer
from app.payouts.base import PayoutInstruction, SignerAttestation, SignerRejected, approval_hash
from app.payouts.mock_signer import MockSigner
from app.services.finance import ledger
from app.services.finance.controls import FinanceError
from app.services.finance.ledger import LedgerError, cr, dr, validate_lines
from app.services.finance.revenue import split_amounts, validate_split
from app.services.finance.wallets import normalize_address, recover_signer, siwe_message
from app.services.finance.withdrawals import evaluate_risk, to_atomic

P = uuid.uuid4()


# ── ledger ───────────────────────────────────────────────────────────────────

def test_balanced_revenue_posting_validates():
    out = validate_lines("JOB_REVENUE_CONFIRMED", "TEST", [
        dr("1020", "100"), cr("2010", "65", P), cr("4045", "15"), cr("4000", "20")])
    assert sum(ln.amount for ln in out if ln.direction == "DEBIT") == Decimal(100)
    assert all(ln.asset == "TEST_USD" for ln in out)


@pytest.mark.parametrize("tx_type,book,lines,msg", [
    ("JOB_REVENUE_CONFIRMED", "TEST", [dr("1020", "100"), cr("2010", "60", P)], "unbalanced"),
    ("MAGIC_MONEY", "TEST", [dr("1000", 1), cr("2000", 1, P)], "unknown transaction type"),
    ("PROVIDER_WITHDRAWAL_HOLD", "TEST", [dr("1000", 1), cr("2040", 1, P)], "may not post"),
    ("TEST_CREDIT", "REAL", [dr("9000", 1), cr("2000", 1, P)], "only allowed on the TEST book"),
    ("PROVIDER_EARNING_AVAILABLE", "TEST", [dr("2010", 1), cr("2000", 1, P)], "needs an owner"),
    ("PROVIDER_EARNING_AVAILABLE", "TEST", [dr("2010", 0, P), cr("2000", 0, P)], "positive"),
    ("PROVIDER_EARNING_AVAILABLE", "TEST", [dr("2010", 1, P, "USDC"), cr("2000", 1, P, "USDC")], "cannot carry REAL"),
    ("PROVIDER_EARNING_AVAILABLE", "REAL", [dr("2010", 1, P, "TEST_USD"), cr("2000", 1, P, "TEST_USD")],
     "cannot carry TEST"),
    ("PROVIDER_EARNING_AVAILABLE", "TEST", [dr("2010", 1, P)], "at least two"),
])
def test_invalid_postings_rejected(tx_type, book, lines, msg):
    with pytest.raises(LedgerError, match=msg):
        validate_lines(tx_type, book, lines)


def test_chart_covers_spec_99_and_types_cover_spec_100():
    codes = {c["code"] for c in ledger.chart_dict()}
    assert {"1000", "1010", "1020", "1030", "1040", "1050", "2000", "2010", "2020", "2030", "2040", "4000", "4010",
            "4020", "4030", "4040", "4050", "5000", "5070", "6000", "6010"} <= codes
    assert {"CUSTOMER_FUNDING", "CUSTOMER_JOB_CHARGE", "JOB_REVENUE_CONFIRMED", "PROVIDER_EARNING_PENDING",
            "PROVIDER_EARNING_AVAILABLE", "PROVIDER_WITHDRAWAL_HOLD", "PROVIDER_WITHDRAWAL_SETTLED", "CHAIN_FEE",
            "TOKEN_CONVERSION", "TREASURY_TRANSFER", "REFUND", "REVERSAL", "CHARGEBACK",
            "RESERVE_ALLOCATION"} <= set(ledger.TX_RULES)


# ── split ────────────────────────────────────────────────────────────────────

def test_split_sums_exactly_to_gross():
    gross = Decimal("0.002")
    p, i, f = split_amounts(gross, {"provider": 0.65, "infrastructure": 0.15, "farmos": 0.20})
    assert p == Decimal("0.0013") and i == Decimal("0.0003") and p + i + f == gross
    p, i, f = split_amounts(Decimal("1") / 3, {"provider": 0.333, "infrastructure": 0.333, "farmos": 0.334})
    assert p + i + f == ledger.d(Decimal(1) / 3)


@pytest.mark.parametrize("bad", [{"provider": 0.7, "infrastructure": 0.2, "farmos": 0.2},
                                 {"provider": 1.1, "infrastructure": -0.1, "farmos": 0.0},
                                 {"provider": 1.0}])
def test_invalid_split_rejected(bad):
    with pytest.raises(ValueError):
        validate_split(bad)


# ── withdrawal risk (spec §74) ───────────────────────────────────────────────

def test_risk_decisions():
    base = dict(wallet_changed_recently=False, amount=10, normal_30d=None, account_age_days=30,
                auth_anomaly=False, unresolved_risk=False)
    assert evaluate_risk(**base)[0] == "STANDARD_HOLD"
    assert evaluate_risk(**{**base, "wallet_changed_recently": True})[0] == "EXTENDED_HOLD"
    assert evaluate_risk(**{**base, "wallet_changed_recently": True, "unresolved_risk": True})[0] == "MANUAL_REVIEW"
    d, score, reasons = evaluate_risk(**{**base, "amount": 400, "normal_30d": 100, "account_age_days": 1})
    assert d == "EXTENDED_HOLD" and score == pytest.approx(0.30) and len(reasons) == 2


def test_to_atomic():
    assert to_atomic(Decimal("12.345678")) == 12345678
    with pytest.raises(FinanceError):
        to_atomic(Decimal("0.0000001"))


# ── mock signer (spec §108) ──────────────────────────────────────────────────

def _ins_att(**over):
    wid, pid, rcpt = str(uuid.uuid4()), str(uuid.uuid4()), "0x" + "ab" * 20
    approvers = (str(uuid.uuid4()),)
    chain = over.pop("chain_id", 84532)
    ah = approval_hash(withdrawal_id=wid, provider_id=pid, recipient=rcpt, amount_atomic=5_000_000, asset="USDC",
                       chain_id=chain, approvers=list(approvers))
    ins = dict(payout_id=str(uuid.uuid4()), withdrawal_id=wid, provider_id=pid, asset="USDC", network="base-sepolia",
               chain_id=chain, token_contract=None, recipient=rcpt, amount_atomic=5_000_000,
               created_at=datetime.now(timezone.utc).isoformat(), approval_hash=ah)
    att = dict(withdrawal_status="APPROVED", wallet_verified=True, wallet_address=rcpt,
               approved_amount_atomic=5_000_000, approvers=approvers, daily_total_atomic_before=0,
               emergency_stop=False, token_status="ACTIVE", token_decimals=6,
               allowed_networks={"base-sepolia": 84532}, daily_limit_atomic=10_000_000, max_tx_atomic=8_000_000)
    for k, v in over.items():
        (ins if k in ins else att)[k] = v
    return PayoutInstruction(**ins), SignerAttestation(**att)


def test_signer_signs_valid_instruction_once():
    s = MockSigner()
    ins, att = _ins_att()
    signed = s.sign(ins, att)
    assert signed.signature.startswith("mock-sig:") and signed.signer == "mock"
    with pytest.raises(SignerRejected, match="DUPLICATE_PAYOUT_ID"):
        s.sign(ins, att)


@pytest.mark.parametrize("over,code,retryable", [
    ({"emergency_stop": True}, "EMERGENCY_STOP", True),
    ({"chain_id": 8453}, "UNSUPPORTED_CHAIN", False),           # Base mainnet: never
    ({"chain_id": 1}, "UNSUPPORTED_CHAIN", False),
    ({"token_status": "PAUSED"}, "UNSUPPORTED_TOKEN", False),
    ({"wallet_verified": False}, "WALLET_NOT_VERIFIED", False),
    ({"withdrawal_status": "SECURITY_HOLD"}, "WITHDRAWAL_NOT_APPROVED", False),
    ({"wallet_address": "0x" + "cd" * 20}, "RECIPIENT_MISMATCH", False),
    ({"approved_amount_atomic": 4_000_000}, "AMOUNT_MISMATCH", False),
    ({"approvers": ("someone-else",)}, "APPROVAL_HASH_MISMATCH", False),
    ({"max_tx_atomic": 1_000_000}, "TRANSACTION_LIMIT_EXCEEDED", False),
    ({"daily_total_atomic_before": 6_000_000}, "DAILY_LIMIT_EXCEEDED", True),
])
def test_signer_rejections(over, code, retryable):
    ins, att = _ins_att(**over)
    with pytest.raises(SignerRejected) as ei:
        MockSigner().sign(ins, att)
    assert ei.value.code == code and ei.value.retryable is retryable


def test_only_mock_signer_and_simulated_provider(monkeypatch):
    from app.config import settings
    assert get_signer().name == "mock" and get_provider().simulated is True
    monkeypatch.setattr(settings, "PAYOUT_SIGNER", "hsm")
    with pytest.raises(RuntimeError):
        get_signer()
    monkeypatch.setattr(settings, "PAYOUT_PROVIDER", "base-mainnet")
    with pytest.raises(RuntimeError):
        get_provider()


# ── SIWE / EIP-191 ───────────────────────────────────────────────────────────

def _msg(addr):
    now = datetime.now(timezone.utc)
    return siwe_message(address=addr, chain_id=84532, nonce="abc123", account_ref="FARM-PROVIDER-X",
                        issued_at=now, expires_at=now + timedelta(minutes=10), request_id="r1")


def test_siwe_recover_matches_address_and_rejects_others():
    acct = Account.create()  # ephemeral, test-only
    msg = _msg(acct.address)
    assert "does not authorize a transfer" in msg and "Chain ID: 84532" in msg
    sig = acct.sign_message(encode_defunct(text=msg)).signature.hex()
    assert recover_signer(msg, sig) == acct.address
    assert recover_signer(msg, "0x" + sig.removeprefix("0x")) == acct.address
    other = Account.create()
    assert recover_signer(msg, other.sign_message(encode_defunct(text=msg)).signature.hex()) != acct.address
    tampered = msg.replace("abc123", "zzz999")
    assert recover_signer(tampered, sig) != acct.address
    with pytest.raises(FinanceError):
        recover_signer(msg, "0x1234")


def test_address_normalization():
    a = Account.create().address
    assert normalize_address(a.lower()) == a
    bad = a[:2] + "".join(c.swapcase() if c.isalpha() else c for c in a[2:])
    if bad != a and bad[2:] != bad[2:].lower() and bad[2:] != bad[2:].upper():
        with pytest.raises(FinanceError):
            normalize_address(bad)
    with pytest.raises(FinanceError):
        normalize_address("0x123")
