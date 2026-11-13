"""Developer / customer API (Sprint 16): FARMOS's own task supply.

Auth: either a user JWT (dashboard; all scopes) or a scoped API key
(``Authorization: Bearer fos_...`` or ``X-API-Key: fos_...``). Key management
always requires a JWT so a leaked key cannot mint or revoke keys.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.core import User
from app.db.models.developer import (
    Customer, CustomerApiKey, CustomerJobCharge, CustomerTopup, CustomerWebhookDelivery, Deployment,
    StripeWebhookEvent,
)
from app.db.models.jobs_ledger import Job, JobResult, JobVerification
from app.db.session import get_db
from app.dependencies import get_current_user, require_roles, security
from app.services.developer import accounts, billing, deployments, idempotency, pricing, stripe_client
from app.services.developer.accounts import Principal
from app.services.finance.ledger import BOOKS

router = APIRouter()
admin_router = APIRouter()


# ── auth ────────────────────────────────────────────────────────────────────

async def get_principal(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    token = x_api_key or (credentials.credentials if credentials else None)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated (JWT or API key required)")
    if token.startswith(accounts.KEY_PREFIX):
        p = await accounts.authenticate_key(db, token)
        if p is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid, revoked or expired API key")
        return p
    user = await get_current_user(credentials, db)
    cust = await accounts.customer_for_user(db, user.id)
    if cust is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no developer account yet: POST /api/v1/developer/account")
    return Principal(customer=cust, user_id=user.id, via="jwt", scopes=set(accounts.SCOPES))


def need(scope: str):
    async def dep(p: Principal = Depends(get_principal)) -> Principal:
        if not p.has(scope):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"API key lacks scope {scope}")
        return p
    return dep


async def jwt_customer(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> tuple[User, Customer]:
    cust = await accounts.customer_for_user(db, user.id)
    if cust is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no developer account yet: POST /api/v1/developer/account")
    return user, cust


def _err(exc: Exception) -> HTTPException:
    if isinstance(exc, deployments.DeploymentError):
        return HTTPException(exc.status_code, exc.detail)
    if isinstance(exc, billing.InsufficientFunds):
        return HTTPException(status.HTTP_402_PAYMENT_REQUIRED,
                             {"error": "insufficient_funds", "available_usd": str(exc.available),
                              "required_usd": str(exc.required), "hint": "top up via POST /api/v1/developer/topups"})
    if isinstance(exc, billing.CustomerOnHold):
        return HTTPException(status.HTTP_403_FORBIDDEN, f"account on hold: {exc}")
    if isinstance(exc, stripe_client.StripeError):
        return HTTPException(exc.status_code, exc.detail)
    if isinstance(exc, idempotency.IdempotencyConflict):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    raise exc


# ── account + keys ──────────────────────────────────────────────────────────

class AccountRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)


def _account_dict(c: Customer) -> dict[str, Any]:
    return {"customer_id": str(c.id), "name": c.name, "status": c.status, "hold_reason": c.hold_reason,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "billing": stripe_client.status(), "scopes_available": list(accounts.SCOPES)}


@router.post("/account")
async def create_account(req: AccountRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cust, created = await accounts.ensure_customer(db, user, req.name)
    await db.commit()
    return {**_account_dict(cust), "created": created}


@router.get("/account")
async def get_account(p: Principal = Depends(get_principal)):
    return {**_account_dict(p.customer), "auth": p.via, "scopes": sorted(p.scopes)}


@router.get("/billing/status")
async def billing_status():
    return stripe_client.status()


class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: Optional[list[str]] = None
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


@router.post("/keys", status_code=201)
async def create_key(req: KeyRequest, uc=Depends(jwt_customer), db: AsyncSession = Depends(get_db)):
    user, cust = uc
    try:
        row, full = await accounts.create_key(db, cust, req.name, req.scopes, req.expires_in_days, user.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    await db.commit()
    return {**accounts.key_dict(row), "api_key": full,
            "warning": "store this key now - FARMOS keeps only a hash and cannot show it again"}


@router.get("/keys")
async def list_keys(uc=Depends(jwt_customer), db: AsyncSession = Depends(get_db)):
    _, cust = uc
    rows = (await db.execute(select(CustomerApiKey).where(CustomerApiKey.customer_id == cust.id)
                             .order_by(desc(CustomerApiKey.created_at)))).scalars().all()
    return [accounts.key_dict(r) for r in rows]


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: uuid.UUID, uc=Depends(jwt_customer), db: AsyncSession = Depends(get_db)):
    user, cust = uc
    try:
        row = await accounts.revoke_key(db, cust, key_id, user.id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "key not found")
    await db.commit()
    return accounts.key_dict(row)


# ── quotes ──────────────────────────────────────────────────────────────────

class QuoteRequest(BaseModel):
    workload_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=1, le=1000)
    runs: Optional[int] = Field(default=None, ge=1, le=100000)


@router.post("/quotes")
async def create_quote(req: QuoteRequest, p: Principal = Depends(need("jobs:read")), db: AsyncSession = Depends(get_db)):
    try:
        payload = deployments.normalize_payload(req.workload_type, req.payload)
        resources = deployments.normalize_resources(req.resources)
    except deployments.DeploymentError as exc:
        raise _err(exc)
    q = await pricing.create_quote(db, p.customer.id, req.workload_type, payload, resources, req.priority)
    await db.commit()
    return {**pricing.quote_dict(q, req.runs), "normalized_payload": payload, "resources": resources,
            "priority": req.priority, "book": stripe_client.book()}


# ── deployments ─────────────────────────────────────────────────────────────

class DeploymentRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    workload_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] = Field(default_factory=dict)
    destination: dict[str, Any] = Field(default_factory=lambda: {"type": "api"})
    priority: int = Field(default=100, ge=1, le=1000)
    quote_id: Optional[uuid.UUID] = None
    max_price_usd: Optional[float] = Field(default=None, gt=0)


async def _dep_for(db: AsyncSession, p: Principal, dep_id: uuid.UUID, lock: bool = False) -> Deployment:
    q = select(Deployment).where(Deployment.id == dep_id, Deployment.customer_id == p.customer.id)
    if lock:
        q = q.with_for_update()
    dep = (await db.execute(q)).scalar_one_or_none()
    if dep is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not found")
    return dep


@router.post("/deployments", status_code=201)
async def create_deployment(req: DeploymentRequest, p: Principal = Depends(need("jobs:write")),
                            db: AsyncSession = Depends(get_db),
                            idem_key: Optional[str] = Header(default=None, alias="Idempotency-Key", max_length=128)):
    if req.destination.get("type") == "webhook" and not p.has("webhooks:write"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "webhook destinations need scope webhooks:write")
    body = req.model_dump(mode="json")
    scope = f"{p.customer.id}:deployments"
    try:
        replay = await idempotency.lookup(db, scope, idem_key, body)
    except idempotency.IdempotencyConflict as exc:
        raise _err(exc)
    if replay:
        return JSONResponse(status_code=replay[0], content={**replay[1], "idempotent_replay": True})
    try:
        dep, secret = await deployments.create(
            db, p.customer, name=req.name or req.workload_type, workload_type=req.workload_type,
            payload=req.payload, resources=req.resources, schedule=req.schedule, destination=req.destination,
            priority=req.priority, quote_id=req.quote_id, max_price_usd=req.max_price_usd, actor_id=p.user_id)
    except (deployments.DeploymentError, billing.InsufficientFunds, billing.CustomerOnHold) as exc:
        await db.rollback()
        raise _err(exc)
    out = deployments.deployment_dict(dep, stats=await deployments.stats_for(db, dep.id))
    out["balance"] = await billing.balance_dict(db, p.customer, dep.book)
    if secret:
        out["webhook_secret"] = secret
        out["webhook_secret_warning"] = "shown once; verify FARMOS-Signature with it"
    idempotency.remember(db, scope, idem_key, body, 201, {k: v for k, v in out.items() if k != "webhook_secret"})
    try:
        await db.commit()
    except IntegrityError:  # concurrent request with the same Idempotency-Key won
        await db.rollback()
        replay = await idempotency.lookup(db, scope, idem_key, body)
        if replay:
            return JSONResponse(status_code=replay[0], content={**replay[1], "idempotent_replay": True})
        raise
    return {**out, "idempotent_replay": False}


@router.get("/deployments")
async def list_deployments(p: Principal = Depends(need("jobs:read")), db: AsyncSession = Depends(get_db),
                           status_filter: Optional[str] = Query(default=None, alias="status"),
                           limit: int = Query(default=50, ge=1, le=200)):
    q = select(Deployment).where(Deployment.customer_id == p.customer.id)
    if status_filter:
        q = q.where(Deployment.status == status_filter.upper())
    rows = (await db.execute(q.order_by(desc(Deployment.created_at)).limit(limit))).scalars().all()
    return [deployments.deployment_dict(d, stats=await deployments.stats_for(db, d.id)) for d in rows]


@router.get("/deployments/{dep_id}")
async def get_deployment(dep_id: uuid.UUID, p: Principal = Depends(need("jobs:read")), db: AsyncSession = Depends(get_db)):
    dep = await _dep_for(db, p, dep_id)
    return deployments.deployment_dict(dep, stats=await deployments.stats_for(db, dep.id))


@router.post("/deployments/{dep_id}/cancel")
async def cancel_deployment(dep_id: uuid.UUID, p: Principal = Depends(need("jobs:write")),
                            db: AsyncSession = Depends(get_db)):
    dep = await _dep_for(db, p, dep_id, lock=True)
    res = await deployments.cancel(db, dep, p.user_id)
    await db.commit()
    return {**deployments.deployment_dict(dep, stats=await deployments.stats_for(db, dep.id)), **res}


@router.get("/deployments/{dep_id}/runs")
async def deployment_runs(dep_id: uuid.UUID, p: Principal = Depends(need("jobs:read")),
                          db: AsyncSession = Depends(get_db), limit: int = Query(default=50, ge=1, le=500),
                          offset: int = Query(default=0, ge=0)):
    dep = await _dep_for(db, p, dep_id)
    return {"deployment_id": str(dep.id), "runs": await deployments.runs(db, dep, limit, offset)}


@router.get("/deployments/{dep_id}/webhook-deliveries")
async def deployment_deliveries(dep_id: uuid.UUID, p: Principal = Depends(need("jobs:read")),
                                db: AsyncSession = Depends(get_db)):
    dep = await _dep_for(db, p, dep_id)
    rows = (await db.execute(select(CustomerWebhookDelivery).where(CustomerWebhookDelivery.deployment_id == dep.id)
                             .order_by(desc(CustomerWebhookDelivery.created_at)).limit(100))).scalars().all()
    return [{"delivery_id": str(r.id), "event_type": r.event_type, "status": r.status, "attempts": r.attempts,
             "last_status_code": r.last_status_code, "last_error": r.last_error,
             "job_id": str(r.job_id) if r.job_id else None,
             "next_attempt_at": r.next_attempt_at.isoformat() if r.next_attempt_at else None,
             "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None} for r in rows]


@router.get("/jobs/{job_id}")
async def job_result(job_id: uuid.UUID, p: Principal = Depends(need("jobs:read")), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(
        select(CustomerJobCharge, Job, JobResult, JobVerification).join(Job, Job.id == CustomerJobCharge.job_id)
        .outerjoin(JobResult, JobResult.job_id == Job.id).outerjoin(JobVerification, JobVerification.job_id == Job.id)
        .where(CustomerJobCharge.job_id == job_id, CustomerJobCharge.customer_id == p.customer.id))).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    ch, job, res, ver = row
    return {**deployments.run_dict(ch, job, res, ver), "deployment_id": str(ch.deployment_id)}


# ── balance + top-ups ───────────────────────────────────────────────────────

@router.get("/balance")
async def balance(p: Principal = Depends(need("billing:read")), db: AsyncSession = Depends(get_db)):
    return await billing.balance_dict(db, p.customer, stripe_client.book())


@router.get("/ledger")
async def customer_ledger(p: Principal = Depends(need("billing:read")), db: AsyncSession = Depends(get_db),
                          limit: int = Query(default=100, ge=1, le=500)):
    return await billing.customer_ledger(db, p.customer.id, stripe_client.book(), limit)


class TopupRequest(BaseModel):
    amount_usd: float = Field(gt=0)
    method: str = Field(default="checkout", pattern="^(checkout|payment_intent)$")


def _topup_dict(t: CustomerTopup) -> dict[str, Any]:
    return {"topup_id": str(t.id), "amount_usd": f"{t.amount_cents / 100:.2f}", "currency": t.currency,
            "method": t.method, "status": t.status, "livemode": t.livemode, "book": t.book,
            "checkout_url": t.checkout_url if t.status in ("PENDING",) else None,
            "funded_usd": str(t.funded_usd) if t.funded_usd is not None else None,
            "refunded_usd": str(t.refunded_usd or 0),
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "funded_at": t.funded_at.isoformat() if t.funded_at else None}


@router.post("/topups", status_code=201)
async def create_topup(req: TopupRequest, p: Principal = Depends(need("billing:write")),
                       db: AsyncSession = Depends(get_db),
                       idem_key: Optional[str] = Header(default=None, alias="Idempotency-Key", max_length=128)):
    """Starts a Stripe payment. The balance is credited ONLY when the signed
    Stripe webhook confirms payment - never on the browser redirect."""
    if not settings.TOPUP_MIN_USD <= req.amount_usd <= settings.TOPUP_MAX_USD:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"amount_usd must be between {settings.TOPUP_MIN_USD} and {settings.TOPUP_MAX_USD}")
    if p.customer.status != "ACTIVE":
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"account is {p.customer.status.lower()}")
    try:
        stripe_client.require_enabled()
    except stripe_client.StripeError as exc:
        raise _err(exc)
    body = req.model_dump(mode="json")
    scope = f"{p.customer.id}:topups"
    try:
        replay = await idempotency.lookup(db, scope, idem_key, body)
    except idempotency.IdempotencyConflict as exc:
        raise _err(exc)
    if replay:
        return JSONResponse(status_code=replay[0], content={**replay[1], "idempotent_replay": True})
    cents = int(round(req.amount_usd * 100))
    t = CustomerTopup(id=uuid.uuid4(), customer_id=p.customer.id, amount_cents=cents, currency="usd",
                      method=req.method, status="CREATED", livemode=stripe_client.mode() == "live",
                      book=stripe_client.book(), refunded_usd=0)
    db.add(t)
    await db.commit()
    client_secret = None
    try:
        if req.method == "checkout":
            email = None
            if p.customer.user_id:
                email = (await db.execute(select(User.email).where(User.id == p.customer.user_id))).scalar()
            s = await stripe_client.create_checkout_session(topup_id=str(t.id), customer_id=str(p.customer.id),
                                                           amount_cents=cents, email=email)
            t.stripe_checkout_session_id, t.checkout_url = s.get("id"), s.get("url")
            if isinstance(s.get("payment_intent"), str):
                t.stripe_payment_intent_id = s["payment_intent"]
        else:
            pi = await stripe_client.create_payment_intent(topup_id=str(t.id), customer_id=str(p.customer.id),
                                                          amount_cents=cents)
            t.stripe_payment_intent_id, client_secret = pi.get("id"), pi.get("client_secret")
    except stripe_client.StripeError as exc:
        t.status = "FAILED"
        await db.commit()
        raise _err(exc)
    t.status = "PENDING"
    out = {**_topup_dict(t), "note": "balance is credited when Stripe's signed webhook confirms payment"}
    idempotency.remember(db, scope, idem_key, body, 201, out)
    await db.commit()
    if client_secret:
        out["client_secret"] = client_secret  # never persisted / replayed
    return {**out, "idempotent_replay": False}


@router.get("/topups")
async def list_topups(p: Principal = Depends(need("billing:read")), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CustomerTopup).where(CustomerTopup.customer_id == p.customer.id)
                             .order_by(desc(CustomerTopup.created_at)).limit(100))).scalars().all()
    return [_topup_dict(t) for t in rows]


# ── admin ───────────────────────────────────────────────────────────────────

ADMIN = require_roles("OWNER", "ADMIN")
FINANCE = require_roles("OWNER", "ADMIN", "FINANCE")


@admin_router.get("/customers")
async def admin_customers(_=Depends(FINANCE), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Customer).order_by(desc(Customer.created_at)).limit(200))).scalars().all()
    book = stripe_client.book()
    return [{**_account_dict(c), "balance": await billing.balance_dict(db, c, book)} for c in rows]


class HoldRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@admin_router.post("/customers/{customer_id}/hold")
async def admin_hold(customer_id: uuid.UUID, req: HoldRequest, _=Depends(ADMIN), db: AsyncSession = Depends(get_db)):
    c = (await db.execute(select(Customer).where(Customer.id == customer_id).with_for_update())).scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "customer not found")
    c.status, c.hold_reason = "ON_HOLD", req.reason
    await db.commit()
    return _account_dict(c)


@admin_router.post("/customers/{customer_id}/release")
async def admin_release(customer_id: uuid.UUID, _=Depends(ADMIN), db: AsyncSession = Depends(get_db)):
    c = (await db.execute(select(Customer).where(Customer.id == customer_id).with_for_update())).scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "customer not found")
    c.status, c.hold_reason = "ACTIVE", None
    for dep in (await db.execute(select(Deployment).where(Deployment.customer_id == c.id,
                                                          Deployment.status == "ON_HOLD"))).scalars():
        dep.status, dep.status_reason = "PAUSED_FUNDS", "account released; resumes when funded"
    await db.commit()
    return _account_dict(c)


@admin_router.get("/reconcile")
async def admin_reconcile(book: str = Query(default="TEST"), _=Depends(FINANCE), db: AsyncSession = Depends(get_db)):
    if book not in BOOKS:
        raise HTTPException(400, f"book must be one of {BOOKS}")
    return await billing.reconcile(db, book)


@admin_router.post("/tick")
async def admin_tick(_=Depends(ADMIN), db: AsyncSession = Depends(get_db)):
    rep = await deployments.tick(db)
    await db.commit()
    return rep


@admin_router.get("/stripe-events")
async def admin_stripe_events(_=Depends(FINANCE), db: AsyncSession = Depends(get_db),
                              limit: int = Query(default=100, ge=1, le=500)):
    rows = (await db.execute(select(StripeWebhookEvent).order_by(desc(StripeWebhookEvent.received_at))
                             .limit(limit))).scalars().all()
    return [{"stripe_event_id": r.stripe_event_id, "event_type": r.event_type, "livemode": r.livemode,
             "processing_status": r.processing_status, "detail": r.detail,
             "related_customer_id": str(r.related_customer_id) if r.related_customer_id else None,
             "related_topup_id": str(r.related_topup_id) if r.related_topup_id else None,
             "payload_hash": r.payload_hash, "received_at": r.received_at.isoformat() if r.received_at else None,
             "processed_at": r.processed_at.isoformat() if r.processed_at else None} for r in rows]


@admin_router.get("/topups/{topup_id}/processor-reconciliation")
async def admin_processor_reconciliation(topup_id: uuid.UUID, _=Depends(FINANCE),
                                         db: AsyncSession = Depends(get_db)):
    from app.services.developer.processor_reconciliation import reconcile_topup
    topup = (await db.execute(select(CustomerTopup).where(CustomerTopup.id == topup_id))).scalar_one_or_none()
    if topup is None:
        raise HTTPException(404, "top-up not found")
    try:
        return await reconcile_topup(topup)
    except stripe_client.StripeError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
