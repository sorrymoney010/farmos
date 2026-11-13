from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.services.developer import processor_reconciliation as recon, stripe_client


@pytest.fixture
def sample():
    topup = SimpleNamespace(id=uuid4(), customer_id=uuid4(), book='TEST', livemode=False,
        amount_cents=2000, currency='usd', funded_usd='20.00', refunded_usd=0,
        status='SUCCEEDED', stripe_payment_intent_id='pi_example')
    evidence = dict(id='pi_example', object='payment_intent', status='succeeded', livemode=False,
        currency='usd', amount_received=2000, client_secret='must-not-leak',
        metadata={'farmos_topup_id': str(topup.id), 'farmos_customer_id': str(topup.customer_id)},
        latest_charge=dict(id='ch_example', object='charge', payment_intent='pi_example',
            livemode=False, paid=True, captured=True, currency='usd', amount=2000,
            amount_captured=2000, disputed=False, amount_refunded=0,
            balance_transaction=dict(id='txn_example', object='balance_transaction',
                source='ch_example', type='charge', currency='usd', amount=2000, fee=88,
                net=1912, status='available', available_on=1700000000)))
    return topup, evidence


def test_matched_evidence_is_not_bank_cash_or_usdc(sample):
    report = recon.compare(*sample)
    assert report['status'] == 'MATCHED'
    assert report['matched_fee_cents'] == 88
    assert report['matched_net_cents'] == 1912
    for field in ['bank_receipt_verified', 'usdc_receipt_verified', 'withdrawal_authorized', 'ledger_modified']:
        assert report[field] is False
    assert 'must-not-leak' not in str(report)


@pytest.mark.parametrize('path,value,issue', [
    ('currency', 'eur', 'payment_currency_mismatch'),
    ('livemode', True, 'processor_mode_mismatch'),
    ('id', 'pi_other', 'payment_intent_mismatch'),
    ('amount_received', 1900, 'payment_amount_mismatch'),
    ('status', 'processing', 'payment_not_succeeded'),
    ('metadata.farmos_customer_id', 'other', 'customer_metadata_mismatch'),
    ('metadata.farmos_topup_id', 'other', 'topup_metadata_mismatch'),
    ('latest_charge.payment_intent', 'pi_other', 'charge_payment_mismatch'),
    ('latest_charge.amount_captured', 1000, 'capture_amount_mismatch'),
    ('latest_charge.disputed', True, 'dispute_review_required'),
    ('latest_charge.amount_refunded', 100, 'refund_review_required'),
    ('latest_charge.balance_transaction.source', 'ch_other', 'balance_source_mismatch'),
    ('latest_charge.balance_transaction.net', 2000, 'balance_amount_mismatch'),
    ('latest_charge.balance_transaction.fee', 88.0, 'balance_amount_mismatch'),
    ('latest_charge.balance_transaction.currency', 'eur', 'balance_currency_mismatch'),
    ('latest_charge.balance_transaction.status', 'unknown', 'unknown_processor_availability'),
    ('latest_charge.balance_transaction.type', 'payout', 'unsupported_balance_type'),
])
def test_mismatches_excluded_from_matched_amounts(sample, path, value, issue):
    topup, evidence = sample
    target = evidence
    keys = path.split('.')
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    report = recon.compare(topup, evidence)
    assert report['status'] == 'REVIEW_REQUIRED'
    assert issue in report['issues']
    assert report['matched_net_cents'] is None


def test_missing_expansion_does_not_pass(sample):
    topup, evidence = sample
    evidence['latest_charge'] = 'ch_example'
    assert recon.compare(topup, evidence)['status'] == 'REVIEW_REQUIRED'


def test_pending_processor_amount_is_not_settlement(sample):
    topup, evidence = sample
    evidence['latest_charge']['balance_transaction']['status'] = 'pending'
    report = recon.compare(topup, evidence)
    assert report['status'] == 'MATCHED'
    assert report['processor_status'] == 'pending'
    assert not report['bank_receipt_verified']


def test_local_refund_and_mode_mismatch(sample):
    topup, evidence = sample
    topup.refunded_usd = '1.00'
    topup.livemode = True
    report = recon.compare(topup, evidence)
    assert {'refund_review_required', 'local_mode_mismatch'} <= set(report['issues'])


@pytest.mark.asyncio
async def test_fixed_origin_get_only_and_replay_no_writes(sample, monkeypatch):
    topup, evidence = sample
    monkeypatch.setattr(settings, 'STRIPE_SECRET_KEY', 'sk_test_fake')
    monkeypatch.setattr(settings, 'STRIPE_API_BASE', 'https://untrusted.invalid')
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.url.host == 'api.stripe.com'
        assert request.url.params['expand[]'] == 'latest_charge.balance_transaction'
        return httpx.Response(200, json=deepcopy(evidence))
    monkeypatch.setattr(stripe_client, 'TRANSPORT', httpx.MockTransport(handler))
    for _ in range(2):
        report = await recon.reconcile_topup(topup)
        assert report['status'] == 'MATCHED'
        assert not report['ledger_modified']
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_wrong_book_fails_before_network(sample, monkeypatch):
    topup, _ = sample
    monkeypatch.setattr(settings, 'STRIPE_SECRET_KEY', 'sk_test_fake')
    topup.book = 'REAL'
    retrieve = AsyncMock()
    monkeypatch.setattr(stripe_client, 'retrieve_payment_evidence', retrieve)
    with pytest.raises(stripe_client.StripeError, match='mode'):
        await recon.reconcile_topup(topup)
    retrieve.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('id', ['pi_x/../../secrets', 'https://bad.test', '', 'pi_x?expand=secret'])
async def test_rejects_path_injection(id):
    with pytest.raises(stripe_client.StripeError, match='identifier'):
        await stripe_client.retrieve_payment_evidence(id)


@pytest.mark.asyncio
async def test_processor_errors_do_not_leak_body(monkeypatch):
    monkeypatch.setattr(settings, 'STRIPE_SECRET_KEY', 'sk_test_fake')
    monkeypatch.setattr(stripe_client, 'TRANSPORT', httpx.MockTransport(
        lambda req: httpx.Response(401, json={'secret': 'must-not-leak'})))
    with pytest.raises(stripe_client.StripeError) as error:
        await stripe_client.retrieve_payment_evidence('pi_example')
    assert 'must-not-leak' not in str(error.value)


@pytest.mark.asyncio
async def test_internal_reconcile_cannot_pass_unbalanced_ledger(monkeypatch):
    from unittest.mock import MagicMock
    from app.services.developer import billing
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalar.return_value = 0
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    monkeypatch.setattr(billing.ledger, 'balance', AsyncMock(return_value=0))
    monkeypatch.setattr(billing.ledger, 'unbalanced_transactions', AsyncMock(return_value=['bad-tx']))
    report = await billing.reconcile(db, 'TEST')
    assert report['ok'] is False
    assert report['scope'] == 'internal_ledger_only'
    assert report['processor_verified'] is False


@pytest.mark.asyncio
async def test_disabled_live_key_cannot_fetch_evidence(monkeypatch):
    monkeypatch.setattr(settings, 'STRIPE_SECRET_KEY', 'sk_live_fake')
    monkeypatch.setattr(settings, 'STRIPE_LIVE_MODE_ENABLED', False)
    with pytest.raises(stripe_client.StripeError, match='live'):
        await stripe_client.retrieve_payment_evidence('pi_example')
