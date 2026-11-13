"""Customer deployments: FARMOS's own task supply for the phone fleet.

A deployment is a customer-owned, priced, prepaid specification of work
(workload + payload + resources + schedule + result destination). Each run
becomes an ordinary FARMOS ``Job`` that goes through the normal scheduler,
lease, verification and economics pipeline, so every hardware-protection rule
(thermal / battery / charging / lease limits) applies unchanged.

Per run: Job (customer_price_usd = locked quote price) + RESERVED charge.
Charge on PRIMARY verification, release on terminal failure. Results are
available via the API and, optionally, pushed to a signed customer webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.audit import AuditLog
from app.db.models.developer import (
    Customer, CustomerJobCharge, CustomerWebhookDelivery, Deployment, PriceQuote,
)
from app.db.models.enums import AuditEventType, JobEventActorType, JobStatus
from app.db.models.jobs_ledger import Job, JobResult, JobVerification
from app.services import economics_service as econ
from app.services import lease_service as leases
from app.services import opportunity_service as opps
from app.services import profitability as prof
from app.services.developer import billing, pricing, stripe_client
from app.security.public_http import PublicHTTPTransport, public_address
from app.services.finance.ledger import d
from app.workloads import cpu_data

WORKLOADS = ("http_check", "monitor", "checksum", "compression", "json_transform")
ACTIVE_STATES = ("ACTIVE", "PAUSED_FUNDS")
QUEUEABLE = (JobStatus.CREATED, JobStatus.VALIDATED, JobStatus.QUEUED)
RESOURCE_KEYS = {"cpu_pct", "ram_mb", "bandwidth_mbps", "tier", "min_farm_score"}
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None  # tests inject a MockTransport


class DeploymentError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── validation ──────────────────────────────────────────────────────────────

BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home", ".corp", ".intranet")


def check_url(url: Any, *, field: str = "url") -> str:
    """Customer URLs run on volunteers' phones inside home networks: only public
    https endpoints, no IP literals in private/loopback/link-local ranges, no
    internal hostnames, no credentials in the URL."""
    if not isinstance(url, str) or len(url) > 2048:
        raise DeploymentError(f"{field} must be a URL string (max 2048 chars)")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise DeploymentError(f"{field} is malformed") from exc
    if parts.scheme != "https":
        raise DeploymentError(f"{field} must use https")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or parts.username or parts.password or "%" in host:
        raise DeploymentError(f"{field} must have a host and no credentials")
    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES) or ("." not in host.strip("[]") and ":" not in host):
        raise DeploymentError(f"{field} host {host!r} is not a public hostname")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None and not public_address(str(ip)):
        raise DeploymentError(f"{field} points at a non-public address")
    if port is not None and port not in (443, 8443):
        raise DeploymentError(f"{field} port {port} not allowed (443/8443)")
    return url.strip()


def normalize_payload(workload_type: str, payload: Optional[dict]) -> dict[str, Any]:
    if workload_type not in WORKLOADS:
        raise DeploymentError(f"workload_type must be one of {', '.join(WORKLOADS)}")
    p = dict(payload or {})
    if workload_type == "http_check":
        method = str(p.get("method", "GET")).upper()
        if method not in ("GET", "HEAD"):
            raise DeploymentError("http_check method must be GET or HEAD")
        timeout = p.get("timeout", 10)
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
            raise DeploymentError("timeout must be 1-30 seconds")
        return {"url": check_url(p.get("url")), "method": method, "timeout": timeout}
    if workload_type == "monitor":
        targets = p.get("targets")
        if not isinstance(targets, list) or not targets:
            raise DeploymentError("monitor needs a non-empty targets list")
        if len(targets) > settings.DEPLOYMENT_MAX_MONITOR_TARGETS:
            raise DeploymentError(f"at most {settings.DEPLOYMENT_MAX_MONITOR_TARGETS} monitor targets")
        return {"targets": [check_url(t, field=f"targets[{i}]") for i, t in enumerate(targets)]}
    try:
        cpu_data.validate_payload(workload_type, p)
    except cpu_data.WorkloadError as exc:
        raise DeploymentError(f"invalid payload: {exc}") from exc
    return p


def normalize_resources(resources: Optional[dict]) -> dict[str, Any]:
    r = {k: v for k, v in (resources or {}).items() if k in RESOURCE_KEYS and v is not None}
    unknown = set((resources or {}).keys()) - RESOURCE_KEYS
    if unknown:
        raise DeploymentError(f"unknown resource keys: {', '.join(sorted(unknown))}")
    if "tier" in r:
        r["tier"] = str(r["tier"]).upper()
        if r["tier"] not in prof.TIER_MULTIPLIER:
            raise DeploymentError("tier must be STANDARD|PRIORITY|ENTERPRISE")
    for k, lo, hi in (("cpu_pct", 1, 100), ("ram_mb", 16, 4096), ("bandwidth_mbps", 0.01, 100),
                      ("min_farm_score", 0, 100)):
        if k in r and (not isinstance(r[k], (int, float)) or not lo <= r[k] <= hi):
            raise DeploymentError(f"{k} must be between {lo} and {hi}")
    return r


def _parse_dt(v: Any, field: str) -> Optional[datetime]:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError as exc:
            raise DeploymentError(f"{field} must be ISO-8601") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_schedule(schedule: Optional[dict]) -> dict[str, Any]:
    """{} = run once now; {interval_seconds, max_runs?, start_at?, end_at?} = recurring."""
    s = dict(schedule or {})
    unknown = set(s) - {"interval_seconds", "max_runs", "start_at", "end_at"}
    if unknown:
        raise DeploymentError(f"unknown schedule keys: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    start, end = _parse_dt(s.get("start_at"), "start_at"), _parse_dt(s.get("end_at"), "end_at")
    iv = s.get("interval_seconds")
    if iv is None:
        out["max_runs"] = 1
        if s.get("max_runs") not in (None, 1):
            raise DeploymentError("max_runs > 1 requires interval_seconds")
    else:
        if not isinstance(iv, int) or iv < settings.DEPLOYMENT_MIN_INTERVAL_SECONDS:
            raise DeploymentError(f"interval_seconds must be an integer >= {settings.DEPLOYMENT_MIN_INTERVAL_SECONDS}")
        out["interval_seconds"] = iv
        mr = s.get("max_runs")
        if mr is not None and (not isinstance(mr, int) or not 1 <= mr <= settings.DEPLOYMENT_MAX_RUNS):
            raise DeploymentError(f"max_runs must be 1-{settings.DEPLOYMENT_MAX_RUNS}")
        if mr is None and end is None:
            raise DeploymentError("recurring deployments need max_runs or end_at (bounded spend)")
        if mr is not None:
            out["max_runs"] = mr
    if start:
        out["start_at"] = start.isoformat()
    if end:
        if end <= (start or utcnow()):
            raise DeploymentError("end_at must be in the future and after start_at")
        out["end_at"] = end.isoformat()
    return out


def normalize_destination(dest: Optional[dict]) -> dict[str, Any]:
    dest = dict(dest or {"type": "api"})
    t = dest.get("type", "api")
    if t == "api":
        return {"type": "api"}
    if t == "webhook":
        return {"type": "webhook", "url": check_url(dest.get("url"), field="destination.url")}
    raise DeploymentError("destination.type must be 'api' or 'webhook'")


# ── create / cancel ─────────────────────────────────────────────────────────

async def _lock_price(db: AsyncSession, customer: Customer, workload_type: str, payload: dict, resources: dict,
                      priority: int, quote_id: Optional[uuid.UUID], max_price: Optional[float]) -> PriceQuote:
    now = utcnow()
    if quote_id is not None:
        q = (await db.execute(select(PriceQuote).where(PriceQuote.id == quote_id,
                                                       PriceQuote.customer_id == customer.id))).scalar_one_or_none()
        if q is None:
            raise DeploymentError("quote not found", 404)
        if q.expires_at <= now:
            raise DeploymentError("quote expired; request a new quote", 409)
        if q.spec_hash != pricing.spec_hash(workload_type, payload, resources, priority):
            raise DeploymentError("quote does not match this deployment spec", 409)
        return q
    if max_price is None:
        raise DeploymentError("provide quote_id (from POST /developer/quotes) or max_price_usd")
    q = await pricing.create_quote(db, customer.id, workload_type, payload, resources, priority)
    if float(q.estimated_cost_usd) > float(max_price):
        raise DeploymentError(f"price {float(q.estimated_cost_usd):.6f} exceeds max_price_usd {max_price}", 409)
    return q


async def create(db: AsyncSession, customer: Customer, *, name: str, workload_type: str, payload: Optional[dict],
                 resources: Optional[dict], schedule: Optional[dict], destination: Optional[dict], priority: int,
                 quote_id: Optional[uuid.UUID], max_price_usd: Optional[float],
                 actor_id: Optional[uuid.UUID]) -> tuple[Deployment, Optional[str]]:
    if customer.status != "ACTIVE":
        raise DeploymentError(f"account is {customer.status.lower()}: {customer.hold_reason or ''}".strip(), 403)
    if stripe_client.mode() == "live_disabled":
        raise DeploymentError("billing is misconfigured (live key with live mode disabled)", 503)
    if not 1 <= priority <= 1000:
        raise DeploymentError("priority must be 1-1000")
    payload = normalize_payload(workload_type, payload)
    resources = normalize_resources(resources)
    sched = normalize_schedule(schedule)
    dest = normalize_destination(destination)
    active = (await db.execute(select(func.count()).select_from(Deployment).where(
        Deployment.customer_id == customer.id, Deployment.status.in_(ACTIVE_STATES)))).scalar() or 0
    if active >= settings.DEPLOYMENT_MAX_ACTIVE_PER_CUSTOMER:
        raise DeploymentError("too many active deployments", 429)
    if not await opps.adapter_enabled(db, opps.INTERNAL_ADAPTER_ID):
        raise DeploymentError("compute supply is paused (kill switch)", 503)
    q = await _lock_price(db, customer, workload_type, payload, resources, priority, quote_id, max_price_usd)
    book = stripe_client.book()
    secret = ("whsec_farmos_" + secrets.token_urlsafe(24)) if dest["type"] == "webhook" else None
    now = utcnow()
    start = _parse_dt(sched.get("start_at"), "start_at") or now
    dep = Deployment(id=uuid.uuid4(), customer_id=customer.id, name=(name or workload_type)[:200],
                     workload_type=workload_type, payload=payload, resources=resources, schedule=sched,
                     destination=dest, webhook_secret=secret, status="ACTIVE", book=book, quote_id=q.id,
                     price_per_run_usd=q.estimated_cost_usd, max_cost_per_run_usd=q.maximum_cost_usd,
                     priority=priority, runs_created=0, next_run_at=start)
    # Funds check up-front so a broke account gets 402 instead of a silent pause.
    b = await billing.balances(db, customer.id, book)
    if b["available"] < d(q.maximum_cost_usd):
        raise billing.InsufficientFunds(b["available"], d(q.maximum_cost_usd))
    db.add(dep)
    await db.flush()
    db.add(AuditLog(event_type=AuditEventType.DEPLOYMENT_CREATED, actor_type="USER" if actor_id else "API_KEY",
                    actor_id=actor_id, target_type="DEPLOYMENT", target_id=dep.id,
                    event_metadata={"customer_id": str(customer.id), "workload_type": workload_type,
                                    "price_per_run_usd": str(q.estimated_cost_usd), "schedule": sched, "book": book}))
    if start <= now:
        await _run_once(db, dep, customer, now, raise_on_funds=True)
    return dep, secret


async def cancel(db: AsyncSession, dep: Deployment, actor_id: Optional[uuid.UUID], reason: str = "cancelled by customer") -> dict:
    if dep.status in ("CANCELLED",):
        return {"cancelled_jobs": 0}
    dep.status, dep.status_reason, dep.cancelled_at, dep.next_run_at = "CANCELLED", reason, utcnow(), None
    jobs = (await db.execute(select(Job).join(CustomerJobCharge, CustomerJobCharge.job_id == Job.id).where(
        CustomerJobCharge.deployment_id == dep.id, Job.status.in_(QUEUEABLE)).with_for_update(of=Job))).scalars().all()
    for j in jobs:
        leases.add_event(db, j.id, j.status.value, JobStatus.CANCELLED.value, reason=reason,
                         actor_type=JobEventActorType.CUSTOMER, actor_id=actor_id)
        j.status = JobStatus.CANCELLED
        await billing.release_for_job(db, j.id, reason)
    db.add(AuditLog(event_type=AuditEventType.DEPLOYMENT_CANCELLED, actor_type="USER" if actor_id else "API_KEY",
                    actor_id=actor_id, target_type="DEPLOYMENT", target_id=dep.id,
                    event_metadata={"cancelled_jobs": len(jobs), "reason": reason}))
    await db.flush()
    return {"cancelled_jobs": len(jobs)}


# ── runs ────────────────────────────────────────────────────────────────────

def _schedule_done(dep: Deployment, now: datetime) -> bool:
    s = dep.schedule or {}
    if dep.runs_created >= int(s.get("max_runs") or settings.DEPLOYMENT_MAX_RUNS):
        return True
    end = _parse_dt(s.get("end_at"), "end_at")
    return end is not None and now >= end


async def _run_once(db: AsyncSession, dep: Deployment, customer: Customer, now: datetime, *,
                    raise_on_funds: bool = False) -> Optional[Job]:
    """Create one run (Job + reservation) atomically; advance the schedule."""
    if _schedule_done(dep, now):
        dep.status, dep.next_run_at = "COMPLETED", None
        return None
    run_index = dep.runs_created + 1
    price, reserve = d(dep.price_per_run_usd), d(dep.max_cost_per_run_usd)
    opp = await opps.internal_opportunity(db, dep.workload_type)
    constraints = {**{k: v for k, v in (dep.resources or {}).items() if k != "min_farm_score"},
                   "customer_price_usd": float(price), "deployment_id": str(dep.id)}
    if "min_farm_score" in (dep.resources or {}):
        constraints["min_farm_score"] = dep.resources["min_farm_score"]
    try:
        async with db.begin_nested():
            job = Job(workload_type=dep.workload_type, status=JobStatus.CREATED, priority=dep.priority,
                      requested_resources=dep.payload, expected_revenue_usd=float(price),
                      max_retries=settings.JOB_DEFAULT_MAX_RETRIES, retry_count=0,
                      idempotency_key=f"dep:{dep.id}:{run_index}", verification_spec={"role": "PRIMARY"},
                      constraints=constraints, opportunity_id=opp.id if opp else None,
                      adapter_id=opps.INTERNAL_ADAPTER_ID, customer_id=customer.user_id,
                      deadline_at=now + timedelta(seconds=settings.DEPLOYMENT_RUN_TTL_SECONDS))
            db.add(job)
            await db.flush()
            await billing.reserve(db, customer=customer, job=job, deployment_id=dep.id, run_index=run_index,
                                  price=price, reserve_amount=reserve, book=dep.book)
            leases.add_event(db, job.id, None, JobStatus.CREATED.value, actor_type=JobEventActorType.CUSTOMER,
                             actor_id=customer.user_id, reason=f"deployment {dep.id} run {run_index}")
            terms = opps.terms_from(opp, dep.workload_type, constraints)
            est = prof.estimate_job(terms, prof.DeviceProfile(), await opps.cost_policy(db),
                                    gross_override=float(price))
            await econ.record_estimate(db, job, None, est, status="ESTIMATED",
                                       opportunity_score=prof.score_opportunity(est, terms, prof.DeviceProfile()),
                                       inputs={"reference_device": True, "deployment_id": str(dep.id)})
            leases.add_event(db, job.id, JobStatus.CREATED.value, JobStatus.VALIDATED.value,
                             reason="deployment run priced and prepaid")
            leases.add_event(db, job.id, JobStatus.VALIDATED.value, JobStatus.QUEUED.value,
                             reason="queued for scheduler")
            job.status = JobStatus.QUEUED
    except billing.InsufficientFunds:
        if raise_on_funds:
            raise
        dep.status, dep.status_reason = "PAUSED_FUNDS", "insufficient prepaid balance; top up to resume"
        await enqueue_event(db, dep, None, "balance.low",
                            {"deployment_id": str(dep.id), "required_usd": str(reserve)})
        return None
    except billing.CustomerOnHold as exc:
        dep.status, dep.status_reason = "ON_HOLD", f"account on hold: {exc}"
        return None
    except IntegrityError:  # run already created by a concurrent tick
        await db.refresh(dep)
        return None
    dep.runs_created = run_index
    dep.last_run_at = now
    iv = (dep.schedule or {}).get("interval_seconds")
    if iv:
        nxt = (dep.next_run_at or now) + timedelta(seconds=iv)
        if nxt <= now:  # fell behind (worker down): no backfill burst, resume cadence from now
            nxt = now + timedelta(seconds=iv)
        dep.next_run_at = nxt
    else:
        dep.next_run_at = None
    if _schedule_done(dep, now):
        dep.status, dep.next_run_at = "COMPLETED", None
    await enqueue_event(db, dep, job.id, "job.created", {"job_id": str(job.id), "run_index": run_index})
    return job


async def tick(db: AsyncSession, now: Optional[datetime] = None, *, deliver: bool = True) -> dict[str, Any]:
    """Create due runs, resume funded paused deployments, settle reservations,
    deliver customer webhooks. Caller commits."""
    now = now or utcnow()
    report: dict[str, Any] = {"runs_created": 0, "paused_funds": 0, "resumed": 0, "completed": 0}
    if await opps.adapter_enabled(db, opps.INTERNAL_ADAPTER_ID):
        due = (await db.execute(select(Deployment).where(
            Deployment.status == "ACTIVE", Deployment.next_run_at.is_not(None), Deployment.next_run_at <= now)
            .order_by(Deployment.next_run_at).limit(settings.DEPLOYMENT_TICK_BATCH)
            .with_for_update(skip_locked=True))).scalars().all()
        customers: dict[uuid.UUID, Customer] = {}
        for dep in due:
            cust = customers.get(dep.customer_id) or (await db.execute(
                select(Customer).where(Customer.id == dep.customer_id))).scalar_one()
            customers[cust.id] = cust
            job = await _run_once(db, dep, cust, now)
            if job is not None:
                report["runs_created"] += 1
            elif dep.status == "PAUSED_FUNDS":
                report["paused_funds"] += 1
            elif dep.status == "COMPLETED":
                report["completed"] += 1
    paused = (await db.execute(select(Deployment).where(Deployment.status == "PAUSED_FUNDS")
                               .limit(settings.DEPLOYMENT_TICK_BATCH).with_for_update(skip_locked=True))).scalars().all()
    for dep in paused:
        b = await billing.balances(db, dep.customer_id, dep.book)
        if b["available"] >= d(dep.max_cost_per_run_usd):
            dep.status, dep.status_reason, dep.next_run_at = "ACTIVE", "resumed after top-up", now
            report["resumed"] += 1
    report["settlement"] = await billing.settle_open_reservations(db)
    if deliver:
        report["webhooks"] = await deliver_pending(db, now)
    await db.flush()
    return report


# ── results ─────────────────────────────────────────────────────────────────

async def runs(db: AsyncSession, dep: Deployment, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    rows = (await db.execute(
        select(CustomerJobCharge, Job, JobResult, JobVerification)
        .join(Job, Job.id == CustomerJobCharge.job_id)
        .outerjoin(JobResult, JobResult.job_id == Job.id)
        .outerjoin(JobVerification, JobVerification.job_id == Job.id)
        .where(CustomerJobCharge.deployment_id == dep.id)
        .order_by(CustomerJobCharge.run_index.desc()).limit(limit).offset(offset))).all()
    return [run_dict(ch, job, res, ver) for ch, job, res, ver in rows]


def run_dict(ch: CustomerJobCharge, job: Job, res: Optional[JobResult], ver: Optional[JobVerification]) -> dict:
    verified = job.status.value in ("VERIFIED", "REVENUE_PENDING", "REVENUE_CONFIRMED", "SETTLED")
    out = {"run_index": ch.run_index, "job_id": str(job.id), "job_status": job.status.value,
           "charge_status": ch.status, "price_usd": f"{float(ch.price_usd):.6f}",
           "charged_usd": f"{float(ch.charged_usd or 0):.6f}", "reserved_usd": f"{float(ch.reserved_usd):.6f}",
           "created_at": job.created_at.isoformat() if job.created_at else None,
           "verification": {"status": ver.status, "method": ver.method} if ver else None,
           "result": None, "result_hash": None}
    if verified and res is not None:
        out["result_hash"] = res.result_hash
        try:
            out["result"] = json.loads(res.result_uri) if res.result_uri else None
        except ValueError:
            out["result"] = None
    if job.last_error and not verified:
        out["error"] = job.last_error[:500]
    return out


def deployment_dict(dep: Deployment, *, stats: Optional[dict] = None) -> dict[str, Any]:
    return {"deployment_id": str(dep.id), "name": dep.name, "workload_type": dep.workload_type,
            "payload": dep.payload, "resources": dep.resources, "schedule": dep.schedule,
            "destination": dep.destination, "status": dep.status, "status_reason": dep.status_reason,
            "book": dep.book, "test_mode": dep.book == "TEST", "priority": dep.priority,
            "quote_id": str(dep.quote_id) if dep.quote_id else None,
            "price_per_run_usd": f"{float(dep.price_per_run_usd):.6f}",
            "max_cost_per_run_usd": f"{float(dep.max_cost_per_run_usd):.6f}",
            "runs_created": dep.runs_created,
            "next_run_at": dep.next_run_at.isoformat() if dep.next_run_at else None,
            "last_run_at": dep.last_run_at.isoformat() if dep.last_run_at else None,
            "created_at": dep.created_at.isoformat() if dep.created_at else None,
            "cancelled_at": dep.cancelled_at.isoformat() if dep.cancelled_at else None,
            **({"stats": stats} if stats is not None else {})}


async def stats_for(db: AsyncSession, dep_id: uuid.UUID) -> dict[str, Any]:
    rows = (await db.execute(select(CustomerJobCharge.status, func.count(),
                                    func.coalesce(func.sum(CustomerJobCharge.charged_usd), 0))
                             .where(CustomerJobCharge.deployment_id == dep_id)
                             .group_by(CustomerJobCharge.status))).all()
    by = {s: (int(n), Decimal(str(v))) for s, n, v in rows}
    return {"runs_reserved": by.get("RESERVED", (0, 0))[0], "runs_charged": by.get("CHARGED", (0, 0))[0],
            "runs_released": by.get("RELEASED", (0, 0))[0],
            "spent_usd": f"{float(by.get('CHARGED', (0, Decimal(0)))[1]):.6f}"}


# ── customer webhooks (spec §135) ───────────────────────────────────────────

async def enqueue_event(db: AsyncSession, dep: Deployment, job_id: Optional[uuid.UUID], event_type: str,
                        data: dict[str, Any]) -> None:
    if (dep.destination or {}).get("type") != "webhook":
        return
    delivery_id = uuid.uuid4()
    payload = {"id": str(delivery_id), "type": event_type, "created": int(time.time()),
               "deployment_id": str(dep.id), "test_mode": dep.book == "TEST", "data": data}
    try:
        async with db.begin_nested():
            db.add(CustomerWebhookDelivery(id=delivery_id, deployment_id=dep.id, job_id=job_id,
                                           event_type=event_type, url=dep.destination["url"], payload=payload,
                                           status="PENDING", attempts=0, next_attempt_at=utcnow()))
            await db.flush()
    except IntegrityError:
        pass  # this event was already enqueued (idempotent)


async def enqueue_job_events(db: AsyncSession, job_ids: list[uuid.UUID], event_type: str) -> None:
    if not job_ids:
        return
    rows = (await db.execute(
        select(CustomerJobCharge, Deployment, Job, JobResult, JobVerification)
        .join(Deployment, Deployment.id == CustomerJobCharge.deployment_id)
        .join(Job, Job.id == CustomerJobCharge.job_id)
        .outerjoin(JobResult, JobResult.job_id == Job.id)
        .outerjoin(JobVerification, JobVerification.job_id == Job.id)
        .where(CustomerJobCharge.job_id.in_(job_ids)))).all()
    for ch, dep, job, res, ver in rows:
        if (dep.destination or {}).get("type") == "webhook":
            await enqueue_event(db, dep, job.id, event_type, run_dict(ch, job, res, ver))


def sign_delivery(secret: str, body: bytes, ts: Optional[int] = None) -> str:
    t = int(ts if ts is not None else time.time())
    return f"t={t},v1=" + hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


async def deliver_pending(db: AsyncSession, now: Optional[datetime] = None, limit: int = 50) -> dict[str, int]:
    now = now or utcnow()
    rows = (await db.execute(
        select(CustomerWebhookDelivery, Deployment).join(Deployment, Deployment.id == CustomerWebhookDelivery.deployment_id)
        .where(CustomerWebhookDelivery.status == "PENDING", CustomerWebhookDelivery.next_attempt_at <= now)
        .order_by(CustomerWebhookDelivery.next_attempt_at).limit(limit)
        .with_for_update(of=CustomerWebhookDelivery, skip_locked=True))).all()
    out = {"delivered": 0, "retrying": 0, "failed": 0}
    if not rows:
        return out
    async with httpx.AsyncClient(transport=TRANSPORT if TRANSPORT is not None else PublicHTTPTransport(),
                                 trust_env=False, timeout=settings.CUSTOMER_WEBHOOK_TIMEOUT_SECONDS,
                                 follow_redirects=False) as client:
        for dl, dep in rows:
            body = json.dumps(dl.payload, separators=(",", ":"), sort_keys=True).encode()
            dl.attempts += 1
            try:
                check_url(dl.url, field="destination.url")  # re-check at send time
                r = await client.post(dl.url, content=body, headers={
                    "Content-Type": "application/json", "User-Agent": "FARMOS-Webhooks/1",
                    "FARMOS-Event-Id": str(dl.id), "FARMOS-Event-Type": dl.event_type,
                    "FARMOS-Signature": sign_delivery(dep.webhook_secret or "", body)})
                dl.last_status_code, dl.last_error = r.status_code, None
                ok = 200 <= r.status_code < 300
            except (httpx.HTTPError, DeploymentError) as exc:
                dl.last_error, ok = f"{type(exc).__name__}: {exc}"[:500], False
            if ok:
                dl.status, dl.delivered_at = "DELIVERED", now
                out["delivered"] += 1
            elif dl.attempts >= settings.CUSTOMER_WEBHOOK_MAX_ATTEMPTS:
                dl.status = "FAILED"
                out["failed"] += 1
            else:
                dl.next_attempt_at = now + timedelta(seconds=30 * 2 ** (dl.attempts - 1))
                out["retrying"] += 1
    await db.flush()
    return out
