"use client";

// FARMOS developer dashboard (Sprint 16): API keys, deployments, results,
// prepaid balance and Stripe top-up. Uses the user's dashboard JWT (same token
// as the main console). Top-ups redirect to Stripe Checkout; the balance is only
// credited when FARMOS receives Stripe's signed webhook - never on redirect.

import { useCallback, useEffect, useMemo, useState } from "react";

const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const WORKLOADS = {
  http_check: { url: "https://example.com/health", method: "GET" },
  monitor: { targets: ["https://example.com", "https://example.org"] },
  checksum: { seed: "demo", size_bytes: 262144, algorithm: "sha256" },
  compression: { seed: "demo", size_bytes: 131072 },
  json_transform: { document: [{ id: 1, v: 3 }, { id: 2, v: 6 }], ops: [{ op: "sum", field: "v" }] },
};
const SCOPES = ["jobs:read", "jobs:write", "billing:read", "billing:write", "webhooks:write"];
const newKey = () => (typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));

function Section({ title, children, action }) {
  return (
    <section className="panel">
      <div className="panel-header"><h2>{title}</h2>{action}</div>
      {children}
    </section>
  );
}

export default function DeveloperPage() {
  const [token, setToken] = useState("");
  const [account, setAccount] = useState(null);
  const [billing, setBilling] = useState(null);
  const [balance, setBalance] = useState(null);
  const [keys, setKeys] = useState([]);
  const [deps, setDeps] = useState([]);
  const [topups, setTopups] = useState([]);
  const [runs, setRuns] = useState({ id: null, rows: [] });
  const [newSecret, setNewSecret] = useState(null);
  const [keyName, setKeyName] = useState("server");
  const [keyScopes, setKeyScopes] = useState(["jobs:read", "jobs:write", "billing:read"]);
  const [wt, setWt] = useState("http_check");
  const [payload, setPayload] = useState(JSON.stringify(WORKLOADS.http_check, null, 2));
  const [interval, setIntervalS] = useState("");
  const [maxRuns, setMaxRuns] = useState("");
  const [webhook, setWebhook] = useState("");
  const [quote, setQuote] = useState(null);
  const [amount, setAmount] = useState("20");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    const saved = window.localStorage.getItem("farmos_token");
    if (saved) setToken(saved);
    const p = new URLSearchParams(window.location.search).get("topup");
    if (p === "success") setMsg("Payment submitted. Your balance updates as soon as Stripe confirms it (webhook).");
    if (p === "cancel") setMsg("Top-up cancelled.");
  }, []);

  const headers = useMemo(() => (token ? { Authorization: `Bearer ${token}`, "Content-Type": "application/json" } : null), [token]);
  const call = useCallback(async (method, path, body, extra = {}) => {
    const r = await fetch(`${apiUrl}/api/v1/developer${path}`, {
      method, headers: { ...headers, ...extra }, body: body === undefined ? undefined : JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e = new Error(typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail || j));
      e.status = r.status;
      throw e;
    }
    return j;
  }, [headers]);

  const load = useCallback(async () => {
    if (!headers) return;
    setErr("");
    try {
      setBilling(await call("GET", "/billing/status"));
      const a = await call("GET", "/account");
      setAccount(a);
      const [b, k, d, t] = await Promise.all([call("GET", "/balance"), call("GET", "/keys"),
        call("GET", "/deployments"), call("GET", "/topups")]);
      setBalance(b); setKeys(k); setDeps(d); setTopups(t);
    } catch (e) {
      if (e.status === 404) setAccount(null);
      else setErr(String(e.message || e));
    }
  }, [headers, call]);
  useEffect(() => { load(); }, [load]);

  const act = async (fn, ok) => {
    setErr(""); setMsg("");
    try { const r = await fn(); if (ok) setMsg(ok(r)); await load(); return r; } catch (e) { setErr(String(e.message || e)); }
    return null;
  };

  const parsedPayload = () => JSON.parse(payload);
  const schedule = () => {
    const s = {};
    if (interval) s.interval_seconds = Number(interval);
    if (maxRuns) s.max_runs = Number(maxRuns);
    return s;
  };

  return (
    <main className="dash">
      <header className="hero">
        <p className="eyebrow">FARMOS · Developers</p>
        <h1>Deploy work to the phone fleet</h1>
        <p className="muted">Prepaid · charged only for verified runs · {billing ? <span className={`badge ${billing.mode === "live" ? "badge-ok" : ""}`}>Stripe {billing.mode}</span> : null} {billing && billing.book === "TEST" ? <span className="badge">TEST book: no real money</span> : null}</p>
        <label className="field">Dashboard token (JWT)
          <input value={token} onChange={(e) => setToken(e.target.value)} placeholder="eyJ..." />
        </label>
        <div className="row">
          <button onClick={() => { window.localStorage.setItem("farmos_token", token); load(); }} disabled={!token}>Load</button>
          {!account && token ? <button className="secondary" onClick={() => act(() => call("POST", "/account", {}), () => "Developer account ready.")}>Create developer account</button> : null}
          <a href="/">← console</a>
        </div>
        {msg ? <p className="status">{msg}</p> : null}
        {err ? <p className="error">{err}</p> : null}
      </header>

      {account ? (
        <div className="grid">
          <Section title="Balance" action={<span className={`badge ${balance && balance.low_balance ? "" : "badge-ok"}`}>{account.status}</span>}>
            {balance ? (
              <table><tbody>
                <tr><td>Balance</td><td className="mono">${balance.balance_usd}</td></tr>
                <tr><td>Reserved (queued runs)</td><td className="mono">${balance.reserved_usd}</td></tr>
                <tr><td>Available</td><td className="mono"><strong>${balance.available_usd}</strong></td></tr>
                <tr><td>Lifetime charged</td><td className="mono">${balance.lifetime_charged_usd}</td></tr>
              </tbody></table>
            ) : <p className="muted">—</p>}
            <div className="row">
              <input value={amount} onChange={(e) => setAmount(e.target.value)} style={{ width: "6rem" }} />
              <button disabled={!billing || !billing.checkout_enabled} onClick={async () => {
                const r = await act(() => call("POST", "/topups", { amount_usd: Number(amount) }, { "Idempotency-Key": newKey() }));
                if (r && r.checkout_url) window.location.href = r.checkout_url;
              }}>Top up with Stripe</button>
            </div>
            {billing && !billing.checkout_enabled ? <p className="muted">{billing.note}</p> : null}
            <h3>Top-ups</h3>
            <table><thead><tr><th>When</th><th>Amount</th><th>Status</th></tr></thead><tbody>
              {topups.slice(0, 8).map((t) => (<tr key={t.topup_id}><td className="muted">{t.created_at}</td><td className="mono">${t.amount_usd}</td><td>{t.status}</td></tr>))}
            </tbody></table>
          </Section>

          <Section title="API keys">
            <div className="row">
              <input value={keyName} onChange={(e) => setKeyName(e.target.value)} placeholder="name" />
              {SCOPES.map((s) => (
                <label key={s} className="muted"><input type="checkbox" checked={keyScopes.includes(s)}
                  onChange={(e) => setKeyScopes(e.target.checked ? [...keyScopes, s] : keyScopes.filter((x) => x !== s))} /> {s}</label>
              ))}
              <button onClick={async () => { const r = await act(() => call("POST", "/keys", { name: keyName, scopes: keyScopes })); if (r) setNewSecret(r.api_key); }}>Create key</button>
            </div>
            {newSecret ? <div className="claim-box"><div>Copy now — shown once:</div><code className="mono">{newSecret}</code></div> : null}
            <table><thead><tr><th>Name</th><th>Prefix</th><th>Scopes</th><th>Last used</th><th></th></tr></thead><tbody>
              {keys.map((k) => (
                <tr key={k.key_id}><td>{k.name}</td><td className="mono">{k.prefix}</td><td className="muted">{k.scopes.join(" ")}</td>
                  <td className="muted">{k.last_used_at || "—"}</td>
                  <td>{k.active ? <button className="secondary" onClick={() => act(() => call("DELETE", `/keys/${k.key_id}`), () => "Key revoked.")}>Revoke</button> : <span className="badge">revoked</span>}</td></tr>
              ))}
            </tbody></table>
          </Section>

          <Section title="New deployment">
            <div className="row">
              <select value={wt} onChange={(e) => { setWt(e.target.value); setPayload(JSON.stringify(WORKLOADS[e.target.value], null, 2)); setQuote(null); }}>
                {Object.keys(WORKLOADS).map((w) => <option key={w}>{w}</option>)}
              </select>
              <input value={interval} onChange={(e) => setIntervalS(e.target.value)} placeholder="every N seconds (blank = once)" />
              <input value={maxRuns} onChange={(e) => setMaxRuns(e.target.value)} placeholder="max runs" />
              <input value={webhook} onChange={(e) => setWebhook(e.target.value)} placeholder="https://… result webhook (optional)" />
            </div>
            <textarea rows={6} style={{ width: "100%" }} className="mono" value={payload} onChange={(e) => { setPayload(e.target.value); setQuote(null); }} />
            <div className="row">
              <button className="secondary" onClick={async () => { const q = await act(() => call("POST", "/quotes", { workload_type: wt, payload: parsedPayload(), runs: maxRuns ? Number(maxRuns) : undefined })); if (q) setQuote(q); }}>Get quote</button>
              <button disabled={!quote} onClick={() => act(() => call("POST", "/deployments", {
                workload_type: wt, payload: parsedPayload(), schedule: schedule(), quote_id: quote.quote_id,
                destination: webhook ? { type: "webhook", url: webhook } : { type: "api" },
              }, { "Idempotency-Key": newKey() }), (r) => (r.webhook_secret ? `Deployed. Webhook secret (shown once): ${r.webhook_secret}` : "Deployed."))}>Deploy at this price</button>
            </div>
            {quote ? <p className="muted">${quote.estimated_cost_usd} per run (max ${quote.maximum_cost_usd} reserved) · ~{quote.estimated_runtime_seconds}s · {quote.fcu} FCU{quote.estimated_total_usd ? ` · total ≈ $${quote.estimated_total_usd}` : ""} · expires {quote.expires_at}</p> : null}
          </Section>

          <Section title="Deployments">
            <table><thead><tr><th>Name</th><th>Workload</th><th>Status</th><th>Runs</th><th>$/run</th><th>Spent</th><th></th></tr></thead><tbody>
              {deps.map((d) => (
                <tr key={d.deployment_id}><td>{d.name}</td><td>{d.workload_type}</td><td>{d.status}</td><td>{d.runs_created}</td>
                  <td className="mono">{d.price_per_run_usd}</td><td className="mono">{d.stats ? d.stats.spent_usd : "—"}</td>
                  <td className="row">
                    <button className="secondary" onClick={async () => { const r = await act(() => call("GET", `/deployments/${d.deployment_id}/runs`)); if (r) setRuns({ id: d.deployment_id, rows: r.runs }); }}>Results</button>
                    {["ACTIVE", "PAUSED_FUNDS"].includes(d.status) ? <button className="secondary" onClick={() => act(() => call("POST", `/deployments/${d.deployment_id}/cancel`), () => "Cancelled.")}>Cancel</button> : null}
                  </td></tr>
              ))}
              {deps.length === 0 ? <tr><td colSpan={7} className="muted">No deployments yet.</td></tr> : null}
            </tbody></table>
            {runs.id ? (
              <>
                <h3>Runs · {runs.id}</h3>
                <table><thead><tr><th>#</th><th>Job</th><th>Charge</th><th>Result</th></tr></thead><tbody>
                  {runs.rows.map((r) => (
                    <tr key={r.job_id}><td>{r.run_index}</td><td>{r.job_status}</td><td>{r.charge_status} ${r.charged_usd}</td>
                      <td className="mono" style={{ maxWidth: "28rem", overflow: "hidden", textOverflow: "ellipsis" }}>{r.result ? JSON.stringify(r.result) : (r.error || "—")}</td></tr>
                  ))}
                </tbody></table>
              </>
            ) : null}
          </Section>
        </div>
      ) : null}
    </main>
  );
}
