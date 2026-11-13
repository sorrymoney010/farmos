"""Read-only comparison of local funding to processor evidence.

Does not post ledger entries or equate Stripe USD with on-chain USDC. Refunds,
disputes and payout/bank evidence need separate accounting before cash release.
"""
from datetime import datetime, timezone
from decimal import Decimal

from app.services.developer import stripe_client


def compare(topup, evidence: dict) -> dict:
    issues = []
    pi = evidence
    charge = pi.get('latest_charge')
    charge = charge if isinstance(charge, dict) else {}
    balance = charge.get('balance_transaction')
    balance = balance if isinstance(balance, dict) else {}
    expected_live = topup.book == 'REAL'
    expected = topup.amount_cents

    def require(condition, issue):
        if not condition:
            issues.append(issue)

    def cents(value):
        return type(value) is int  # reject bool and lossy floating-point inputs

    require(topup.book in ('TEST', 'REAL'), 'unsupported_book')
    require(topup.livemode is expected_live, 'local_mode_mismatch')
    require(topup.currency == 'usd', 'unsupported_local_currency')
    require(cents(expected) and expected > 0, 'invalid_local_amount')
    require(topup.status in ('SUCCEEDED', 'PARTIALLY_REFUNDED', 'REFUNDED'), 'local_funding_not_confirmed')
    require(Decimal(str(topup.funded_usd or 0)) * 100 == expected, 'local_funded_amount_mismatch')
    require(pi.get('id') == topup.stripe_payment_intent_id, 'payment_intent_mismatch')
    require(pi.get('object') == 'payment_intent', 'invalid_payment_object')
    require(pi.get('status') == 'succeeded', 'payment_not_succeeded')
    require(pi.get('livemode') is expected_live, 'processor_mode_mismatch')
    require(pi.get('currency') == 'usd', 'payment_currency_mismatch')
    require(cents(pi.get('amount_received')) and pi['amount_received'] == expected, 'payment_amount_mismatch')
    metadata = pi.get('metadata') or {}
    metadata = metadata if isinstance(metadata, dict) else {}
    require(metadata.get('farmos_topup_id') == str(topup.id), 'topup_metadata_mismatch')
    require(metadata.get('farmos_customer_id') == str(topup.customer_id), 'customer_metadata_mismatch')
    require(charge.get('object') == 'charge' and bool(charge.get('id')), 'missing_charge')
    require(charge.get('payment_intent') == topup.stripe_payment_intent_id, 'charge_payment_mismatch')
    require(charge.get('livemode') is expected_live, 'charge_mode_mismatch')
    require(charge.get('paid') is True and charge.get('captured') is True, 'charge_not_captured')
    require(charge.get('currency') == 'usd', 'charge_currency_mismatch')
    require(cents(charge.get('amount_captured')) and charge['amount_captured'] == expected, 'capture_amount_mismatch')
    require(cents(charge.get('amount')) and charge['amount'] == expected, 'charge_amount_mismatch')
    require(charge.get('disputed') is False, 'dispute_review_required')
    require(cents(charge.get('amount_refunded')) and charge['amount_refunded'] == 0
            and Decimal(str(topup.refunded_usd or 0)) == 0, 'refund_review_required')
    require(balance.get('object') == 'balance_transaction' and bool(balance.get('id')), 'missing_balance_transaction')
    require(balance.get('source') == charge.get('id') and bool(charge.get('id')), 'balance_source_mismatch')
    require(balance.get('type') in ('charge', 'payment'), 'unsupported_balance_type')
    require(balance.get('currency') == 'usd', 'balance_currency_mismatch')
    gross, fee, net = (balance.get(k) for k in ('amount', 'fee', 'net'))
    valid_amounts = all(cents(v) for v in (gross, fee, net))
    require(valid_amounts and gross == expected and fee >= 0 and net >= 0 and gross - fee == net,
            'balance_amount_mismatch')
    require(balance.get('status') in ('available', 'pending'), 'unknown_processor_availability')
    matched = not issues
    return {
        'topup_id': str(topup.id), 'book': topup.book, 'test_mode': not expected_live,
        'currency': 'USD', 'status': 'MATCHED' if matched else 'REVIEW_REQUIRED',
        'issues': issues, 'payment_intent_id': topup.stripe_payment_intent_id,
        'charge_id': charge.get('id'), 'balance_transaction_id': balance.get('id'),
        'expected_gross_cents': expected,
        'matched_gross_cents': gross if matched else None,
        'matched_fee_cents': fee if matched else None,
        'matched_net_cents': net if matched else None,
        'processor_status': balance.get('status'),
        'available_on': balance.get('available_on'),
        'bank_receipt_verified': False, 'usdc_receipt_verified': False,
        'withdrawal_authorized': False, 'ledger_modified': False,
    }


async def reconcile_topup(topup) -> dict:
    stripe_client.require_enabled()
    if stripe_client.book() != topup.book:
        raise stripe_client.StripeError('configured Stripe mode does not match top-up book', 409)
    if not topup.stripe_payment_intent_id:
        raise stripe_client.StripeError('top-up has no payment intent to reconcile', 409)
    evidence = await stripe_client.retrieve_payment_evidence(topup.stripe_payment_intent_id)
    report = compare(topup, evidence)
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    report['evidence_source'] = 'stripe_api'
    return report
