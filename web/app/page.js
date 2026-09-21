"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function Badge({ children, tone = "neutral" }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

function Section({ title, children, action }) {
  return (
    <section className="panel">
      <div className="panel-header">
        <h2>{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

export default function Home() {
  const [token, setToken] = useState("");
  const [claimCode, setClaimCode] = useState(null);
  const [fleet, setFleet] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [revenue, setRevenue] = useState(null);
  const [provider, setProvider] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const saved = typeof window !== "undefined" ? window.localStorage.getItem("farmos_token") : null;
    if (saved) setToken(saved);
  }, []);

  const authHeaders = useMemo(
    () => (token ? { Authorization: `Bearer ${token}`, "Content-Type": "application/json" } : null),
    [token],
  );

  const load = useCallback(async () => {
    if (!authHeaders) {
      setError("Paste a bearer token (OWNER/ADMIN/PROVIDER) to load the dashboard.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      window.localStorage.setItem("farmos_token", token);
      const [fleetRes, jobsRes, revenueRes, providerRes] = await Promise.all([
        fetch(`${apiUrl}/api/v1/admin/fleet`, { headers: authHeaders }),
        fetch(`${apiUrl}/api/v1/jobs/`, { headers: authHeaders }),
        fetch(`${apiUrl}/api/v1/admin/revenue`, { headers: authHeaders }),
        fetch(`${apiUrl}/api/v1/provider/dashboard`, { headers: authHeaders }),
      ]);
      if (!fleetRes.ok) throw new Error(`fleet ${fleetRes.status}`);
      if (!jobsRes.ok) throw new Error(`jobs ${jobsRes.status}`);
      setFleet(await fleetRes.json());
      setJobs(await jobsRes.json());
      setRevenue(revenueRes.ok ? await revenueRes.json() : null);
      setProvider(providerRes.ok ? await providerRes.json() : null);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }, [authHeaders, token]);

  const mintClaim = async () => {
    if (!authHeaders) return;
    setBusy(true);
    setError("");
    try {
      const res = await fetch(`${apiUrl}/api/v1/devices/claim-code`, {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({}),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || `claim-code ${res.status}`);
      setClaimCode(body);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const jobCounts = useMemo(() => {
    const counts = { pending: 0, running: 0, complete: 0, other: 0 };
    for (const j of jobs || []) {
      const st = (j.status || "").toUpperCase();
      if (["CREATED", "QUEUED", "DISPATCHED"].includes(st)) counts.pending += 1;
      else if (["ACCEPTED", "RUNNING", "RESULT_SUBMITTED", "VERIFYING"].includes(st)) counts.running += 1;
      else if (["COMPLETE", "COMPLETED", "VERIFIED", "PAID"].includes(st)) counts.complete += 1;
      else counts.other += 1;
    }
    return counts;
  }, [jobs]);

  const devices = fleet?.devices || provider?.devices || [];
  const earnings = revenue || provider?.earnings || {};

  return (
    <main className="dash">
      <header className="hero">
        <p className="eyebrow">FARMOS</p>
        <h1>V1 Operations Dashboard</h1>
        <p className="status">Devices · Jobs · Earnings · Wireless claim codes</p>
        <p className="muted">
          API: <a href={`${apiUrl}/docs`}>{apiUrl}/docs</a>
        </p>
      </header>

      <Section title="Auth">
        <label className="field">
          Bearer token
          <input
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Paste JWT from /api/v1/auth/login"
            autoComplete="off"
          />
        </label>
        <div className="row">
          <button onClick={load} disabled={busy}>{busy ? "Loading…" : "Refresh dashboard"}</button>
          <button className="secondary" onClick={mintClaim} disabled={busy || !token}>
            Mint claim code
          </button>
        </div>
        {error ? <p className="error">{error}</p> : null}
        {claimCode ? (
          <div className="claim-box">
            <div>
              Claim code: <strong className="mono">{claimCode.claim_code}</strong>
            </div>
            <div className="muted">Expires: {claimCode.expires_at}</div>
            <div className="muted">{claimCode.instructions || claimCode.instructions || ""}</div>
          </div>
        ) : null}
      </Section>

      <div className="grid">
        <Section title="Devices">
          <p className="muted">{devices.length} enrolled · online via status ACTIVE/RUNNING/IDLE</p>
          <table>
            <thead>
              <tr>
                <th>Human ID</th>
                <th>Status</th>
                <th>Last seen</th>
                <th>Score</th>
              </tr>
            </thead>
            <tbody>
              {devices.length === 0 ? (
                <tr><td colSpan={4} className="muted">No devices yet — mint a claim code and enroll over Wi‑Fi.</td></tr>
              ) : (
                devices.map((d) => (
                  <tr key={d.device_id}>
                    <td className="mono">{d.human_id || d.device_id}</td>
                    <td><span className={`badge ${d.status === "ACTIVE" ? "badge-ok" : ""}`}>{d.status}</span></td>
                    <td className="muted">{d.last_seen_at || "—"}</td>
                    <td>{d.farm_score ?? "—"}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </Section>

        <Section title="Jobs">
          <div className="row stats">
            <div><strong>{jobCounts.pending}</strong><span>pending</span></div>
            <div><strong>{jobCounts.running}</strong><span>running</span></div>
            <div><strong>{jobCounts.complete}</strong><span>complete</span></div>
          </div>
          <table>
            <thead>
              <tr>
                <th>Job</th>
                <th>Type</th>
                <th>Status</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {(jobs || []).slice(0, 20).map((j) => (
                <tr key={j.job_id}>
                  <td className="mono">{String(j.job_id).slice(0, 8)}</td>
                  <td>{j.workload_type}</td>
                  <td><span className="badge">{j.status}</span></td>
                  <td className="muted">{j.created_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>

        <Section title="Earnings / ledger">
          <div className="row stats">
            <div><strong>{Number(earnings.total_ledger_credits ?? earnings.total_credits ?? 0).toFixed(4)}</strong><span>credits</span></div>
            <div><strong>{Number(earnings.total_ledger_debits ?? earnings.total_debits ?? 0).toFixed(4)}</strong><span>debits</span></div>
            <div><strong>{Number(earnings.net_position ?? earnings.net ?? 0).toFixed(4)}</strong><span>net</span></div>
          </div>
          <p className="muted">TEST credits stay separate from REAL USDC in the ledger model.</p>
        </Section>
      </div>
    </main>
  );
}