"use client";

// Phase 3 admin views: opportunity registry (scores), adapter kill switch,
// scheduler queue + manual tick, per-device P&L and per-job economics.
// Nothing here moves money: revenue is UNCONFIRMED (internal) or SIMULATED (acurast_sim).

import { useCallback, useEffect, useState } from "react";

const usd = (v, d = 6) => (v == null ? "—" : `$${Number(v).toFixed(d)}`);
const num = (v, d = 3) => (v == null ? "—" : Number(v).toPrecision(d));

export default function RevenueEngine({ apiUrl, authHeaders, refreshKey, Section, Badge }) {
  const [opps, setOpps] = useState([]);
  const [adapters, setAdapters] = useState([]);
  const [queue, setQueue] = useState([]);
  const [devices, setDevices] = useState([]);
  const [jobEcon, setJobEcon] = useState([]);
  const [summary, setSummary] = useState(null);
  const [lastTick, setLastTick] = useState(null);
  const [err, setErr] = useState("");

  const get = useCallback(
    async (path) => {
      const r = await fetch(`${apiUrl}/api/v1${path}`, { headers: authHeaders });
      if (!r.ok) throw new Error(`${path} ${r.status}`);
      return r.json();
    },
    [apiUrl, authHeaders],
  );

  const post = useCallback(
    async (path, body = {}) => {
      const r = await fetch(`${apiUrl}/api/v1${path}`, { method: "POST", headers: authHeaders, body: JSON.stringify(body) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || `${path} ${r.status}`);
      return j;
    },
    [apiUrl, authHeaders],
  );

  const load = useCallback(async () => {
    if (!authHeaders) return;
    setErr("");
    try {
      const [o, a, q, d, j, s] = await Promise.all([
        get("/opportunities/"),
        get("/admin/adapters"),
        get("/admin/scheduler/queue"),
        get("/admin/economics/devices"),
        get("/admin/economics/jobs?limit=25"),
        get("/admin/economics/summary?hours=24"),
      ]);
      setOpps(o.opportunities || []);
      setAdapters(a.adapters || []);
      setQueue(q.queued || []);
      setDevices(d.devices || []);
      setJobEcon(j.jobs || []);
      setSummary(s);
    } catch (e) {
      setErr(String(e.message || e));
    }
  }, [authHeaders, get]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const act = async (fn) => {
    try {
      await fn();
      await load();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  const toggleAdapter = (a) =>
    act(async () => {
      if (a.enabled) {
        const reason = window.prompt(`Kill switch: disable ${a.adapter_id}? Reason:`, "manual kill switch");
        if (reason == null) return;
        await post(`/admin/adapters/${a.adapter_id}/disable`, { reason });
      } else {
        await post(`/admin/adapters/${a.adapter_id}/enable`);
      }
    });

  const tick = (dry_run) => act(async () => setLastTick(await post("/admin/scheduler/tick", { dry_run })));

  const states = summary?.by_revenue_state || {};

  return (
    <>
      {err ? <p className="error">{err}</p> : null}
      <div className="grid">
        <Section title="Revenue adapters · kill switch">
          <table>
            <thead>
              <tr><th>Adapter</th><th>Kind</th><th>Stage</th><th>Health</th><th>Reconcile</th><th>State</th><th /></tr>
            </thead>
            <tbody>
              {adapters.map((a) => (
                <tr key={a.adapter_id}>
                  <td className="mono">{a.adapter_id}{a.simulated ? <> <Badge tone="warn">SIMULATED</Badge></> : null}</td>
                  <td>{a.kind}</td>
                  <td>{a.certification_stage}</td>
                  <td>{a.health_status || "—"}</td>
                  <td className="muted">{a.last_reconcile?.status || "—"}</td>
                  <td>{a.enabled ? <Badge tone="ok">ENABLED</Badge> : <Badge tone="bad">DISABLED</Badge>}
                    {a.disabled_reason ? <div className="muted">{a.disabled_reason}</div> : null}</td>
                  <td>
                    <button className={a.enabled ? "danger" : "secondary"} onClick={() => toggleAdapter(a)}>
                      {a.enabled ? "Kill" : "Enable"}
                    </button>
                    {a.kind === "EXTERNAL" ? (
                      <button className="secondary" onClick={() => act(() => post(`/admin/adapters/${a.adapter_id}/reconcile`))}>
                        Reconcile
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>

        <Section title="Opportunities (ranked)">
          <p className="muted">Reference estimate on a FarmScore-50 / reputation-50 device; token rewards include haircuts.</p>
          <table>
            <thead>
              <tr><th>Opportunity</th><th>Adapter</th><th>Workload</th><th>Gross→adj.</th><th>Cost</th><th>Net / job</th><th>Score</th><th>Status</th></tr>
            </thead>
            <tbody>
              {opps.map((o) => {
                const e = o.reference_estimate || {};
                return (
                  <tr key={o.opportunity_id}>
                    <td>{o.name || o.external_id}</td>
                    <td className="mono">{o.adapter_id}</td>
                    <td>{o.workload_type}</td>
                    <td>{usd(e.gross_usd)} → {usd(e.adjusted_gross_usd)}</td>
                    <td>{usd(e.total_cost_usd)}</td>
                    <td>{e.profitable ? <Badge tone="ok">{usd(e.expected_net_usd)}</Badge> : <Badge tone="bad">{usd(e.expected_net_usd)} reject</Badge>}</td>
                    <td>{num(o.opportunity_score)}</td>
                    <td>
                      <Badge tone={o.status === "ACTIVE" ? "ok" : "bad"}>{o.status}</Badge>
                      <button className="secondary" onClick={() => act(() => post(`/opportunities/${o.opportunity_id}/${o.status === "ACTIVE" ? "disable" : "enable"}`, { reason: "dashboard" }))}>
                        {o.status === "ACTIVE" ? "Disable" : "Enable"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Section>

        <Section
          title={`Scheduler queue (${queue.length})`}
          action={
            <div className="row">
              <button className="secondary" onClick={() => tick(true)}>Dry run</button>
              <button onClick={() => tick(false)}>Run tick</button>
            </div>
          }
        >
          {lastTick ? (
            <p className="muted">
              Last tick{lastTick.dry_run ? " (dry run)" : ""}: {lastTick.dispatched.length} dispatched ·{" "}
              {Object.keys(lastTick.waiting).length} waiting · {lastTick.expired.length} expired · {lastTick.devices_online} devices online
            </p>
          ) : null}
          <table>
            <thead><tr><th>Job</th><th>Type</th><th>Priority</th><th>Retries</th><th>Exp. net</th><th>Waiting because</th></tr></thead>
            <tbody>
              {queue.map((j) => (
                <tr key={j.job_id}>
                  <td className="mono">{j.job_id.slice(0, 8)}</td>
                  <td>{j.workload_type}</td>
                  <td>{j.priority}</td>
                  <td>{j.retry_count}</td>
                  <td>{usd(j.expected_net_usd)}</td>
                  <td className="muted">{lastTick?.waiting?.[j.job_id] || j.last_error || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>

        <Section title="Economics (24h)">
          <div className="row stats">
            {["UNCONFIRMED", "SIMULATED", "CONFIRMED"].map((k) => (
              <div key={k}><strong>{usd(states[k]?.net_usd, 4)}</strong><span>{k.toLowerCase()} net · {states[k]?.jobs || 0} jobs</span></div>
            ))}
            <div><strong>{summary?.rejected_unprofitable ?? 0}</strong><span>rejected unprofitable</span></div>
          </div>
          <p className="muted">{summary?.note}</p>
        </Section>

        <Section title="Device P&L">
          <table>
            <thead><tr><th>Device</th><th>Jobs</th><th>Gross</th><th>Electricity</th><th>Depreciation</th><th>Net</th><th>Payback</th></tr></thead>
            <tbody>
              {devices.slice(0, 50).map((d) => (
                <tr key={d.device_id}>
                  <td className="mono">{d.human_id}</td>
                  <td>{d.jobs_completed}</td>
                  <td>{usd(d.lifetime_gross_usd)}</td>
                  <td>{usd(d.electricity_usd)}</td>
                  <td>{usd(d.depreciation_usd)}</td>
                  <td>{usd(d.net_usd)}</td>
                  <td className="muted">{d.payback_days != null ? `${Number(d.payback_days).toFixed(0)} d` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>

        <Section title="Job economics (latest)">
          <table>
            <thead><tr><th>Job</th><th>Adapter</th><th>Status</th><th>Expected net</th><th>Actual net</th><th>Score</th><th>Revenue</th></tr></thead>
            <tbody>
              {jobEcon.map((j) => (
                <tr key={j.job_id}>
                  <td className="mono">{j.job_id.slice(0, 8)}</td>
                  <td className="mono">{j.adapter_id}</td>
                  <td><Badge tone={j.status === "REJECTED" ? "bad" : j.status === "ACTUAL" ? "ok" : "neutral"}>{j.status}</Badge></td>
                  <td>{usd(j.expected.net_usd)}</td>
                  <td>{usd(j.actual?.net_usd)}</td>
                  <td>{num(j.scores.final_score)}</td>
                  <td className="muted">{j.revenue_state || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      </div>
    </>
  );
}
