"""Fleet simulator + load test (spec §79, §77; DoD item 19).

Drives N fake nodes (default 100) end to end through the *real* HTTP API and
database — the same signed device protocol the Android agent uses:

    enroll (signed, admin enrollment token) -> heartbeat (varied thermal/battery)
    -> benchmark (varied FarmScore) -> admin creates mixed jobs (internal CPU/data
    + simulated external adapter) -> scheduler ticks (concurrently, to stress
    locking) -> nodes poll / accept (signed) / execute / submit (signed hash)
    -> server-side verification (recompute / redundant shadows) -> TEST-book
    revenue confirmation -> release -> adapter + treasury reconciliation.

Node profiles model §79: healthy, warm (YELLOW), hot (RED, must get no work),
low battery off-charger (must get no work), slow, disconnecting, fraudulent
(wrong results -> verification failure -> requeue -> quarantine) and weak
FarmScore. Afterwards it checks invariants straight from the database:

* no device ever holds more than MAX_CONCURRENT_JOBS_PER_DEVICE active leases,
  no job ever has two active leases (sampled every round);
* no job is credited / confirmed / released more than once;
* hot / low-battery nodes received no dispatch after their unsafe heartbeat;
* the TEST ledger trial balance is balanced with zero unbalanced transactions.

Modes:
* in-process (default): ``httpx.ASGITransport`` against ``app.main.app`` with its
  lifespan, using ``DATABASE_URL`` — timing settings are compressed so lease
  expiry / earnings hold complete within the run;
* ``--base-url``: against a running server (timing is the server's own).

Money: TEST book only, simulated billing, simulated adapters. The simulator
refuses to run when ``APP_ENV=production``.

    python -m app.simulator.fleet --nodes 100 --jobs 400 --json report.json
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.workloads import cpu_data

API = "/api/v1"
TERMINAL = {"VERIFIED", "REVENUE_PENDING", "REVENUE_CONFIRMED", "SETTLED", "REJECTED", "EXPIRED", "FAILED",
            "INVALID_RESULT", "CANCELLED", "PAYMENT_FAILED", "QUARANTINED"}
SUCCESS = {"VERIFIED", "REVENUE_PENDING", "REVENUE_CONFIRMED", "SETTLED"}

BASE_RAW = {
    "cpu": {"single_thread_hashes_per_sec": 120000, "multi_thread_hashes_per_sec": 700000, "threads": 8},
    "ai": {"matmul_gflops": 1.8},
    "memory": {"copy_mb_per_sec": 3500.5, "total_mb": 4096, "available_mb": 1500},
    "storage": {"write_mb_per_sec": 80.25, "read_mb_per_sec": 210.0, "free_mb": 20000},
    "network": {"latency_ms": 45.3, "down_mbps": None, "up_mbps": None},
    "thermal": {"start_c": 31.2, "end_c": 33.0, "peak_c": 33.4},
    "battery": {"pct": 88, "charging": True, "health": "GOOD", "temperature_c": 31.2},
    "duration_ms": 9000,
}


# ── node profiles (spec §79) ────────────────────────────────────────────────

@dataclass(frozen=True)
class Profile:
    name: str
    weight: float
    temp: tuple[float, float] = (28.0, 36.0)
    battery: tuple[float, float] = (60.0, 100.0)
    charging: bool = True
    bench_scale: tuple[float, float] = (0.8, 1.6)
    latency: tuple[float, float] = (0.0, 0.05)       # extra completion latency (s)
    fraud: float = 0.0                                # probability a result is tampered
    disconnect: float = 0.0                           # probability to vanish when handed a job
    unsafe: bool = False                              # must never receive work after its heartbeat


PROFILES = (
    Profile("healthy", 0.50),
    Profile("warm_yellow", 0.10, temp=(38.5, 41.5)),
    Profile("slow", 0.10, latency=(0.2, 0.6), bench_scale=(0.5, 0.9)),
    Profile("weak_farmscore", 0.05, bench_scale=(0.25, 0.45)),
    Profile("hot_red", 0.06, temp=(46.0, 48.0), unsafe=True),
    Profile("low_battery", 0.06, battery=(8.0, 13.0), charging=False, unsafe=True),
    Profile("disconnecting", 0.06, disconnect=0.6),
    Profile("fraudulent", 0.07, fraud=1.0),
)


def allocate(n: int, rng: random.Random) -> list[Profile]:
    """Deterministic largest-remainder allocation of ``n`` nodes to profiles."""
    total = sum(p.weight for p in PROFILES)
    raw = [(p, n * p.weight / total) for p in PROFILES]
    counts = {p.name: int(x) for p, x in raw}
    for p, x in sorted(raw, key=lambda t: t[1] - int(t[1]), reverse=True)[: n - sum(counts.values())]:
        counts[p.name] += 1
    out = [p for p in PROFILES for _ in range(counts[p.name])]
    rng.shuffle(out)
    return out


# ── metrics ─────────────────────────────────────────────────────────────────

class Metrics:
    def __init__(self) -> None:
        self.lat: dict[str, list[float]] = defaultdict(list)
        self.status: dict[str, Counter] = defaultdict(Counter)
        self.events: Counter = Counter()

    def record(self, op: str, code: int, seconds: float) -> None:
        self.lat[op].append(seconds)
        self.status[op][code] += 1

    def summary(self) -> dict[str, Any]:
        out = {}
        for op, xs in sorted(self.lat.items()):
            s = sorted(xs)
            errors = sum(c for code, c in self.status[op].items() if code >= 400 or code == 0)
            out[op] = {"requests": len(xs), "errors": errors, "error_rate": round(errors / len(xs), 4),
                       "p50_ms": round(1000 * s[len(s) // 2], 1),
                       "p95_ms": round(1000 * s[min(len(s) - 1, int(len(s) * 0.95))], 1),
                       "max_ms": round(1000 * s[-1], 1), "status": dict(self.status[op])}
        return out


class Api:
    def __init__(self, http: httpx.AsyncClient, metrics: Metrics, sem: asyncio.Semaphore):
        self.http, self.m, self.sem = http, metrics, sem

    async def call(self, op: str, method: str, url: str, **kw) -> httpx.Response:
        async with self.sem:
            t = time.perf_counter()
            try:
                r = await self.http.request(method, API + url, **kw)
            except httpx.HTTPError as exc:
                self.m.record(op, 0, time.perf_counter() - t)
                raise RuntimeError(f"{op}: transport error {exc}") from exc
            self.m.record(op, r.status_code, time.perf_counter() - t)
            return r


def _sign(key, payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.b64encode(key.sign(body, ec.ECDSA(hashes.SHA256()))).decode()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── fake node ───────────────────────────────────────────────────────────────

@dataclass
class SimNode:
    idx: int
    profile: Profile
    rng: random.Random
    key: Any = None
    id: Optional[str] = None
    token: Optional[str] = None
    status: Optional[str] = None
    farm_score: Optional[float] = None
    online: bool = True
    unsafe_since: Optional[datetime] = None
    last_hb: float = 0.0
    stats: Counter = field(default_factory=Counter)

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def telemetry(self, green: bool = False) -> dict[str, Any]:
        if green:
            return {"temperature_c": 31.0, "battery_pct": 90.0, "charging": True, "battery_health": "GOOD"}
        p = self.profile
        return {"temperature_c": round(self.rng.uniform(*p.temp), 1),
                "battery_pct": round(self.rng.uniform(*p.battery), 1),
                "charging": p.charging, "battery_health": "GOOD"}

    async def enroll(self, api: Api, admin_h: dict[str, str]) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        r = await api.call("enrollment_token", "POST", "/devices/enrollment-token", headers=admin_h, json={})
        r.raise_for_status()
        pem = self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        payload = {"enrollment_token": r.json()["enrollment_token"], "device_public_key": pem,
                   "hardware_fingerprint": f"sha256:{uuid4().hex}",
                   "profile": {"manufacturer": "FarmSim", "model": f"SimPhone-{self.profile.name}",
                               "cpu_cores": 8, "ram_mb": 4096},
                   "request_nonce": uuid4().hex, "request_timestamp": _now()}
        payload["signature"] = _sign(self.key, payload)
        r = await api.call("enroll", "POST", "/devices/enroll", json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"enroll failed {r.status_code}: {r.text[:200]}")
        self.id, self.token = r.json()["device_id"], r.json()["node_token"]

    async def heartbeat(self, api: Api, green: bool = False) -> httpx.Response:
        hb = {"observed_at": _now(), "request_nonce": uuid4().hex, "request_timestamp": _now(),
              **self.telemetry(green)}
        hb["signature"] = _sign(self.key, hb)
        r = await api.call("heartbeat", "POST", f"/devices/{self.id}/heartbeat", headers=self.h, json=hb)
        self.last_hb = time.monotonic()
        if r.status_code == 200:
            self.status = r.json().get("status", self.status)
        return r

    async def benchmark(self, api: Api) -> httpx.Response:
        s = self.rng.uniform(*self.profile.bench_scale)
        raw = json.loads(json.dumps(BASE_RAW))
        raw["cpu"]["single_thread_hashes_per_sec"] = int(raw["cpu"]["single_thread_hashes_per_sec"] * s)
        raw["cpu"]["multi_thread_hashes_per_sec"] = int(raw["cpu"]["multi_thread_hashes_per_sec"] * s)
        raw["ai"]["matmul_gflops"] = round(raw["ai"]["matmul_gflops"] * s, 3)
        raw["memory"]["copy_mb_per_sec"] = round(raw["memory"]["copy_mb_per_sec"] * s, 2)
        raw_json = json.dumps(raw, separators=(",", ":"))
        body = {"suite_version": "farmos-bench-1", "job_id": None, "raw_results_json": raw_json,
                "results_sha256": hashlib.sha256(raw_json.encode()).hexdigest(),
                "request_nonce": uuid4().hex, "request_timestamp": _now()}
        signed = {k: body[k] for k in ("request_nonce", "request_timestamp", "suite_version", "results_sha256")}
        signed.update(device_id=self.id, job_id="")
        body["signature"] = _sign(self.key, signed)
        r = await api.call("benchmark", "POST", f"/devices/{self.id}/benchmark/result", headers=self.h, json=body)
        if r.status_code == 200:
            self.status = r.json().get("status", self.status)
            self.farm_score = r.json().get("farm_score", self.farm_score)
        return r

    async def work_once(self, api: Api) -> None:
        """Poll -> accept -> execute -> submit one job (if any)."""
        r = await api.call("poll", "POST", "/jobs/device/jobs/next", headers=self.h)
        if r.status_code != 200:
            self.stats["poll_error"] += 1
            return
        body = r.json()
        job = body.get("job")
        if not job:
            if body.get("paused"):
                self.stats["paused"] += 1
            return
        jid = job["job_id"]
        self.stats["received"] += 1
        if self.profile.disconnect and self.rng.random() < self.profile.disconnect:
            self.online = False  # vanishes holding a DISPATCHED lease -> sweep must requeue it
            self.stats["disconnected"] += 1
            return
        acc = {"request_nonce": uuid4().hex, "request_timestamp": _now(), "job_id": jid, "device_id": self.id}
        a = await api.call("accept", "POST", f"/jobs/device/jobs/{jid}/accept", headers=self.h,
                           json={"request_nonce": acc["request_nonce"], "request_timestamp": acc["request_timestamp"],
                                 "signature": _sign(self.key, acc)})
        if a.status_code != 200:
            self.stats["accept_rejected"] += 1
            return
        result = await asyncio.to_thread(cpu_data.execute, job["workload_type"], job.get("payload") or {})
        lo, hi = self.profile.latency
        if hi:
            await asyncio.sleep(self.rng.uniform(lo, hi))
        if self.profile.fraud and self.rng.random() < self.profile.fraud:
            result = dict(result)
            key = "input_sha256" if "input_sha256" in result else "output_sha256"
            result[key] = "0" * 64
            self.stats["tampered"] += 1
        uri = cpu_data.canonical(result)
        res = {"request_nonce": uuid4().hex, "request_timestamp": _now(), "job_id": jid, "device_id": self.id,
               "result_hash": cpu_data.sha256_hex(uri), "result_uri": uri}
        sub = {k: res[k] for k in ("request_nonce", "request_timestamp", "result_hash", "result_uri")}
        sub["signature"] = _sign(self.key, res)
        s = await api.call("submit", "POST", f"/jobs/device/jobs/{jid}/result", headers=self.h, json=sub)
        if s.status_code == 200:
            st = s.json().get("status")
            self.stats["submitted"] += 1
            self.stats[f"result_{st}"] += 1
        else:
            self.stats["submit_rejected"] += 1


# ── job mix ─────────────────────────────────────────────────────────────────

def job_specs(n: int, rng: random.Random, external_share: float) -> tuple[list[dict[str, Any]], int]:
    n_ext = int(round(n * external_share))
    out = []
    for i in range(n - n_ext):
        kind = ("checksum", "compression", "json_transform")[i % 3]
        seed = f"sim-{uuid4().hex[:12]}"
        if kind == "checksum":
            payload = {"seed": seed, "size_bytes": rng.choice([16384, 65536, 262144]), "algorithm": "sha256"}
        elif kind == "compression":
            payload = {"seed": seed, "size_bytes": rng.choice([8192, 32768, 131072])}
        else:
            doc = [{"id": k, "g": rng.choice("abc"), "v": rng.randint(0, 100)} for k in range(rng.randint(5, 40))]
            payload = {"document": doc, "ops": [{"op": "filter_eq", "field": "g", "value": "a"},
                                                {"op": "select", "fields": ["id", "v"]}]}
        out.append({"workload_type": kind, "payload": payload, "priority": rng.choice([10, 50, 100, 200])})
    return out, n_ext


# ── DB invariants ───────────────────────────────────────────────────────────

class Db:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, poolclass=NullPool)

    async def rows(self, sql: str, **params) -> list[Any]:
        async with self.engine.begin() as conn:
            res = await conn.execute(text(sql), params)
            return res.fetchall() if res.returns_rows else []

    async def close(self) -> None:
        await self.engine.dispose()


def _ids(xs) -> list[str]:
    return [str(x) for x in xs]


async def sample_double_booking(db: Db, device_ids: list[str], max_per_device: int) -> dict[str, int]:
    dev = await db.rows(
        "SELECT device_id, count(*) FROM job_leases WHERE status = 'ACTIVE' AND device_id::text = ANY(:d) "
        "GROUP BY device_id HAVING count(*) > :m", d=_ids(device_ids), m=max_per_device)
    job = await db.rows(
        "SELECT job_id, count(*) FROM job_leases WHERE status = 'ACTIVE' AND device_id::text = ANY(:d) "
        "GROUP BY job_id HAVING count(*) > 1", d=_ids(device_ids))
    return {"devices_over_cap": len(dev), "jobs_multi_leased": len(job)}


# ── the run ─────────────────────────────────────────────────────────────────

@dataclass
class Config:
    nodes: int = 100
    jobs: int = 400
    external_share: float = 0.1
    concurrency: int = 32
    ticks_per_round: int = 2
    timeout_s: float = 300.0
    seed: int = 1337
    isolate: bool = False
    base_url: Optional[str] = None
    database_url: Optional[str] = None
    admin_email: str = "admin@example.com"
    admin_password: str = "ChangeMe123!"
    accelerate: bool = True          # in-process only: compress lease / hold timings
    heartbeat_every_s: float = 5.0
    min_success: float = 0.95        # share of created jobs that must end VERIFIED for a PASS


ACCELERATED = {"LEASE_ACCEPT_TIMEOUT_SECONDS": 4, "PROVIDER_EARNINGS_HOLD_HOURS": 0, "DEVICE_ONLINE_SECONDS": 20,
               "THERMAL_COOLDOWN_SECONDS": 600, "RATE_LIMIT_REQUESTS": 1_000_000}


@asynccontextmanager
async def _client(cfg: Config):
    if cfg.base_url:
        async with httpx.AsyncClient(base_url=cfg.base_url, timeout=60) as c:
            yield c, None
        return
    from app.config import settings
    from app.main import app

    if settings.APP_ENV == "production":
        raise SystemExit("refusing to run the fleet simulator with APP_ENV=production")
    saved = {k: getattr(settings, k) for k in ACCELERATED if hasattr(settings, k)}
    if cfg.accelerate:
        for k, v in ACCELERATED.items():
            if hasattr(settings, k):
                setattr(settings, k, v)
        # one simulated round touches every node; keep the accept window above a round's
        # wall time so large in-process fleets are not starved by their own simulator
        settings.LEASE_ACCEPT_TIMEOUT_SECONDS = max(ACCELERATED["LEASE_ACCEPT_TIMEOUT_SECONDS"], cfg.nodes // 20)
        settings.DEVICE_ONLINE_SECONDS = max(ACCELERATED["DEVICE_ONLINE_SECONDS"], cfg.nodes // 10)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://farmos-sim", timeout=60) as c:
                yield c, settings
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)


async def run(cfg: Config) -> dict[str, Any]:
    rng = random.Random(cfg.seed)
    metrics = Metrics()
    sem = asyncio.Semaphore(cfg.concurrency)
    db_url = cfg.database_url or os.environ.get("DATABASE_URL")
    if not db_url:
        from app.config import settings as _s
        db_url = _s.DATABASE_URL
    db = Db(db_url)
    report: dict[str, Any] = {"config": {k: v for k, v in cfg.__dict__.items() if k != "admin_password"},
                              "started_at": _now()}
    t0 = time.perf_counter()
    try:
        async with _client(cfg) as (http, settings):
            api = Api(http, metrics, sem)
            max_per_device = int(getattr(settings, "MAX_CONCURRENT_JOBS_PER_DEVICE", 1)) if settings else 1
            r = await api.call("login", "POST", "/auth/login",
                               json={"email": cfg.admin_email, "password": cfg.admin_password})
            r.raise_for_status()
            admin = {"Authorization": f"Bearer {r.json()['access_token']}"}

            # 1. enroll + activate (+ unsafe heartbeat for hot / low-battery profiles)
            nodes = [SimNode(i, p, random.Random(rng.random())) for i, p in enumerate(allocate(cfg.nodes, rng))]
            t = time.perf_counter()
            await asyncio.gather(*(n.enroll(api, admin) for n in nodes))
            report["enroll_seconds"] = round(time.perf_counter() - t, 2)

            async def activate(n: SimNode) -> None:
                await n.heartbeat(api, green=True)
                b = await n.benchmark(api)
                n.stats["benchmark_status"] = b.status_code
                hb = await n.heartbeat(api)
                if n.profile.unsafe:
                    n.unsafe_since = datetime.now(timezone.utc)
                    n.stats["unsafe_hb_state"] = 1
                    n.safety = hb.json().get("safety") if hb.status_code == 200 else None
            t = time.perf_counter()
            await asyncio.gather(*(activate(n) for n in nodes))
            report["activate_seconds"] = round(time.perf_counter() - t, 2)
            device_ids = [n.id for n in nodes]
            if cfg.isolate:  # shared DB: make every non-simulated device look offline
                await db.rows("UPDATE devices SET last_seen_at = now() - interval '2 hours' "
                              "WHERE NOT (id::text = ANY(:d))", d=_ids(device_ids))

            # 2. mixed job load (internal CPU/data + simulated external adapter)
            specs, n_ext = job_specs(cfg.jobs, rng, cfg.external_share)
            t = time.perf_counter()
            created = await asyncio.gather(*(api.call("create_job", "POST", "/jobs/", headers=admin, json=s)
                                             for s in specs))
            job_ids = [c.json()["job_id"] for c in created if c.status_code == 200]
            ext_created = 0
            if n_ext:
                opps_ = (await api.call("opportunities", "GET", "/opportunities/", headers=admin)).json()
                tee = next((o for o in opps_["opportunities"] if o["external_id"] == "acu-tee-compute"), None)
                if tee:
                    rr = await api.call("create_ext_jobs", "POST", f"/opportunities/{tee['opportunity_id']}/jobs",
                                        headers=admin, json={"count": n_ext})
                    if rr.status_code == 200:
                        ext = [j["job_id"] for j in rr.json()["created"]]
                        job_ids += ext
                        ext_created = len(ext)
            report["jobs_created"] = {"total": len(job_ids), "internal": len(job_ids) - ext_created,
                                      "external_simulated": ext_created,
                                      "create_seconds": round(time.perf_counter() - t, 2)}

            # 3. work loop
            violations = Counter()
            rounds = 0
            t_work = time.perf_counter()
            pending = set(job_ids)
            while pending and time.perf_counter() - t_work < cfg.timeout_s:
                rounds += 1
                ids = list(pending)
                ticks = [api.call("scheduler_tick", "POST", "/admin/scheduler/tick", headers=admin,
                                  json={"job_ids": ids[:1000], "limit": 1000})
                         for _ in range(cfg.ticks_per_round)]
                await asyncio.gather(*ticks)
                for k, v in (await sample_double_booking(db, device_ids, max_per_device)).items():
                    violations[k] += v
                now_m = time.monotonic()
                live = [n for n in nodes if n.online]

                async def step(n: SimNode) -> None:
                    if now_m - n.last_hb >= cfg.heartbeat_every_s:
                        await n.heartbeat(api)
                    await n.work_once(api)
                await asyncio.gather(*(step(n) for n in live))
                for k, v in (await sample_double_booking(db, device_ids, max_per_device)).items():
                    violations[k] += v
                await api.call("sweep_leases", "POST", "/admin/jobs/sweep-leases", headers=admin)
                st = await db.rows("SELECT id::text, status, dead_lettered FROM jobs WHERE id::text = ANY(:j)",
                                   j=_ids(ids))
                for jid, status, dead in st:
                    if status in TERMINAL or dead:
                        pending.discard(jid)
                if all(not n.online or n.profile.unsafe for n in nodes):
                    break
                if not any(n.stats["received"] for n in live) and rounds > 3:
                    await asyncio.sleep(0.25)  # waiting for accept-lease expiry of vanished nodes
            work_seconds = time.perf_counter() - t_work

            # 4. shadows created for redundant verification belong to the run too
            shadows = await db.rows("SELECT id::text, status FROM jobs WHERE parent_job_id::text = ANY(:j)",
                                    j=_ids(job_ids))

            # 5. finance: TEST-book revenue confirmation, release, reconciliations
            fin: dict[str, Any] = {"confirmed": 0, "released": 0, "held": 0, "skipped": 0}
            for _ in range(50):
                c = await api.call("revenue_confirm", "POST", "/admin/finance/revenue/confirm", headers=admin,
                                   json={"job_ids": job_ids[:2000] if len(job_ids) <= 2000 else None, "limit": 500})
                if c.status_code != 200:
                    fin["confirm_error"] = c.text[:300]
                    break
                cj = c.json()
                fin["confirmed"] += len(cj["confirmed"])
                fin["skipped"] += len(cj.get("skipped") or {})
                fin["book"] = cj.get("book")
                if not cj["confirmed"]:
                    break
            for _ in range(50):  # the endpoint releases up to 500 rows per call
                rel = await api.call("release_pending", "POST", "/admin/finance/revenue/release-pending",
                                     headers=admin)
                if rel.status_code != 200:
                    break
                fin["released"] += len(rel.json()["released"])
                fin["held"] = len(rel.json()["held"])
                if not rel.json()["released"]:
                    break
            ad = await api.call("adapter_reconcile", "POST", "/admin/adapters/acurast_sim/reconcile", headers=admin)
            fin["adapter_reconcile"] = ad.json().get("status") if ad.status_code == 200 else f"HTTP {ad.status_code}"
            tr = await api.call("treasury_reconcile", "POST", "/admin/finance/reconcile?book=TEST", headers=admin)
            fin["treasury_reconcile"] = (tr.json().get("status") if tr.status_code == 200 else f"HTTP {tr.status_code}")
            tb = await api.call("trial_balance", "GET", "/admin/finance/trial-balance?book=TEST", headers=admin)
            tbj = tb.json() if tb.status_code == 200 else {}
            fin["trial_balance_balanced"] = tbj.get("balanced")
            fin["unbalanced_transactions"] = len(tbj.get("unbalanced_transactions") or [])
            fin["totals"] = tbj.get("totals")
            report["finance"] = fin

            # 6. outcome + invariants from the DB
            final = await db.rows("SELECT status, dead_lettered, count(*) FROM jobs WHERE id::text = ANY(:j) "
                                  "GROUP BY status, dead_lettered", j=_ids(job_ids))
            by_status = Counter()
            dead = 0
            for status, dl, c in final:
                by_status[status] += c
                dead += c if dl else 0
            verified = sum(c for s, c in by_status.items() if s in SUCCESS)
            ver = await db.rows("SELECT v.status, count(*) FROM job_verifications v WHERE v.job_id::text = ANY(:j) "
                                "GROUP BY v.status", j=_ids(job_ids + [s[0] for s in shadows]))
            ver_counts = {s: c for s, c in ver}
            requeues = (await db.rows("SELECT count(*) FROM job_events WHERE job_id::text = ANY(:j) "
                                      "AND to_status = 'QUEUED' AND from_status IS NOT NULL "
                                      "AND from_status NOT IN ('CREATED','VALIDATED')", j=_ids(job_ids)))[0][0]
            # a job may be credited / confirmed / released at most once, whichever device ran it
            dup_ledger = await db.rows(
                "SELECT reference_id, tx_type, count(*) FROM ledger_transactions "
                "WHERE reference_type IN ('JOB', 'JOB_RESULT_TEST') "
                "AND reference_id::text = ANY(:j) AND tx_type <> 'REVERSAL' "
                "GROUP BY reference_id, tx_type HAVING count(*) > 1", j=_ids(job_ids + [s[0] for s in shadows]))
            credited = (await db.rows(
                "SELECT count(DISTINCT reference_id) FROM ledger_transactions WHERE tx_type = 'TEST_CREDIT' "
                "AND reference_id::text = ANY(:j)", j=_ids(job_ids)))[0][0]
            unsafe_dispatch = 0
            for n in nodes:
                if n.unsafe_since is not None:
                    unsafe_dispatch += (await db.rows(
                        "SELECT count(*) FROM job_assignments WHERE device_id = CAST(:d AS uuid) AND assigned_at > :t",
                        d=n.id, t=n.unsafe_since))[0][0]
            dev = await db.rows("SELECT id::text, status FROM devices WHERE id::text = ANY(:d)",
                                d=_ids(device_ids))
            dev_status = {i: s for i, s in dev}
            prof_rows: dict[str, Counter] = defaultdict(Counter)
            for n in nodes:
                pr = prof_rows[n.profile.name]
                pr["nodes"] += 1
                pr[f"status_{dev_status.get(n.id)}"] += 1
                for k in ("received", "submitted", "result_VERIFIED", "tampered", "disconnected", "paused",
                          "accept_rejected", "submit_rejected"):
                    pr[k] += n.stats[k]
            fraud_nodes = [n for n in nodes if n.profile.fraud]
            quarantined_fraud = sum(1 for n in fraud_nodes if dev_status.get(n.id) == "QUARANTINED")
            honest_quarantined = sum(1 for n in nodes if not n.profile.fraud and dev_status.get(n.id) == "QUARANTINED")
            # every submission is verified server-side; the submit response carries the outcome
            sub_results = Counter()
            for n in nodes:
                for k, v in n.stats.items():
                    if k.startswith("result_"):
                        sub_results[k[len("result_"):]] += v
            submissions = sum(sub_results.values())
            failed_ver = submissions - sub_results.get("VERIFIED", 0)
            report.update({
                "rounds": rounds, "work_seconds": round(work_seconds, 2),
                "jobs_outcome": {"by_status": dict(by_status), "dead_lettered": dead, "verified": verified,
                                 "unfinished": len(pending), "requeues": requeues,
                                 "redundant_shadows": len(shadows)},
                "throughput": {"verified_jobs_per_s": round(verified / work_seconds, 2) if work_seconds else None,
                               "requests_per_s": round(sum(len(v) for v in metrics.lat.values())
                                                       / (time.perf_counter() - t0), 1)},
                "verification": {"submissions": submissions, "submission_outcomes": dict(sub_results),
                                 "failed_submissions": failed_ver,
                                 "failure_rate": round(failed_ver / max(1, submissions), 4),
                                 "final_verification_rows": ver_counts,
                                 "job_success_rate": round(verified / max(1, len(job_ids)), 4)},
                "quarantine": {"fraud_nodes": len(fraud_nodes), "fraud_quarantined": quarantined_fraud,
                               "honest_quarantined": honest_quarantined},
                "profiles": {k: dict(v) for k, v in sorted(prof_rows.items())},
                "invariants": {
                    "double_booking_device_samples": violations["devices_over_cap"],
                    "double_booking_job_samples": violations["jobs_multi_leased"],
                    "duplicate_ledger_postings": len(dup_ledger), "jobs_credited": credited,
                    "unsafe_node_dispatches": unsafe_dispatch,
                    "ledger_balanced": bool(fin.get("trial_balance_balanced")) and fin["unbalanced_transactions"] == 0,
                },
                "http": metrics.summary(),
            })
            inv = report["invariants"]
            report["passed"] = (inv["double_booking_device_samples"] == 0 and inv["double_booking_job_samples"] == 0
                                and inv["duplicate_ledger_postings"] == 0
                                and inv["unsafe_node_dispatches"] == 0 and inv["ledger_balanced"]
                                and honest_quarantined == 0 and not pending
                                and verified >= cfg.min_success * len(job_ids))
            report["pass_criteria"] = {"min_success": cfg.min_success, "no_unfinished_jobs": not pending,
                                       "no_honest_node_quarantined": honest_quarantined == 0}
    finally:
        await db.close()
    report["total_seconds"] = round(time.perf_counter() - t0, 2)
    return report


def format_report(r: dict[str, Any]) -> str:
    jo, th, ver, inv, fin = r["jobs_outcome"], r["throughput"], r["verification"], r["invariants"], r["finance"]
    lines = [
        f"FarmOS fleet simulator — {r['config']['nodes']} nodes, {r['jobs_created']['total']} jobs "
        f"({r['jobs_created']['external_simulated']} simulated-external), {r['rounds']} rounds",
        f"  time: enroll {r['enroll_seconds']}s, activate {r['activate_seconds']}s, work {r['work_seconds']}s, "
        f"total {r['total_seconds']}s",
        f"  throughput: {th['verified_jobs_per_s']} verified jobs/s, {th['requests_per_s']} HTTP req/s",
        f"  jobs: verified {jo['verified']}, dead-lettered {jo['dead_lettered']}, unfinished {jo['unfinished']}, "
        f"requeues {jo['requeues']}, redundant shadows {jo['redundant_shadows']}, by status {jo['by_status']}",
        f"  verification: {ver['submissions']} submissions {ver['submission_outcomes']}, "
        f"failure rate {ver['failure_rate']:.2%}, "
        f"job success {ver['job_success_rate']:.2%}",
        f"  quarantine: {r['quarantine']}",
        f"  finance ({fin.get('book')}): confirmed {fin['confirmed']}, released {fin['released']}, held {fin['held']}, "
        f"adapter reconcile {fin.get('adapter_reconcile')}, treasury {fin.get('treasury_reconcile')}, "
        f"trial balance balanced={fin.get('trial_balance_balanced')} unbalanced_tx={fin['unbalanced_transactions']}",
        f"  invariants: {inv}",
        "  http (p50/p95 ms, error rate):",
    ]
    for op, s in r["http"].items():
        lines.append(f"    {op:20s} n={s['requests']:5d} p50={s['p50_ms']:7.1f} p95={s['p95_ms']:7.1f} "
                     f"err={s['error_rate']:.2%} {s['status']}")
    lines.append(f"  RESULT: {'PASS' if r['passed'] else 'FAIL'}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nodes", type=int, default=100)
    ap.add_argument("--jobs", type=int, default=400)
    ap.add_argument("--external-share", type=float, default=0.1)
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--ticks-per-round", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--isolate", action="store_true", help="mark non-simulated devices offline (shared DBs)")
    ap.add_argument("--base-url", default=None, help="run against a live server instead of in-process")
    ap.add_argument("--database-url", default=None, help="DB used for invariant checks (default DATABASE_URL)")
    ap.add_argument("--no-accelerate", action="store_true")
    ap.add_argument("--min-success", type=float, default=0.95, help="required verified share for PASS")
    ap.add_argument("--json", default=None, help="write the full report to this file")
    a = ap.parse_args(argv)
    if not 1 <= a.nodes <= 1000:
        ap.error("--nodes must be in [1, 1000] (spec §79)")
    cfg = Config(nodes=a.nodes, jobs=a.jobs, external_share=a.external_share, concurrency=a.concurrency,
                 ticks_per_round=a.ticks_per_round, timeout_s=a.timeout, seed=a.seed, isolate=a.isolate,
                 base_url=a.base_url, database_url=a.database_url, accelerate=not a.no_accelerate,
                 min_success=a.min_success,
                 admin_email=os.environ.get("FARMOS_SIM_ADMIN_EMAIL", "admin@example.com"),
                 admin_password=os.environ.get("FARMOS_SIM_ADMIN_PASSWORD", "ChangeMe123!"))
    report = asyncio.run(run(cfg))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(report, f, indent=2, default=str)
    print(format_report(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
