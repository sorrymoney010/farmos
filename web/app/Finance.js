"use client";

// Phase 4 finance views. TEST book + simulated testnet only: nothing here moves real money.
// Wallet ownership is proven by signing a SIWE (EIP-4361 / EIP-191) message in the user's own
// wallet (window.ethereum personal_sign). FARMOS never asks for a seed phrase or private key.

import { useCallback, useEffect, useState } from "react";

const amt = (v, d = 6) => (v == null ? "—" : Number(v).toFixed(d));

function useApi(apiUrl, authHeaders) {
  const call = useCallback(
    async (method, path, body, extraHeaders = {}) => {
      const r = await fetch(`${apiUrl}/api/v1${path}`, {
        method,
        headers: { ...authHeaders, ...extraHeaders },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail ? (typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail)) : `${path} ${r.status}`);
      return j;
    },
    [apiUrl, authHeaders],
  );
  return call;
}

const newKey = () => (typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));

export function ProviderFinance({ apiUrl, authHeaders, refreshKey, Section, Badge }) {
  const call = useApi(apiUrl, authHeaders);
  const [bal, setBal] = useState(null);
  const [wallets, setWallets] = useState([]);
  const [wds, setWds] = useState([]);
  const [ledger, setLedger] = useState([]);
  const [address, setAddress] = useState("");
  const [network, setNetwork] = useState("base-sepolia");
  const [amount, setAmount] = useState("");
  const [walletId, setWalletId] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    if (!authHeaders) return;
    setErr("");
    try {
      const [b, w, x, l] = await Promise.all([
        call("GET", "/provider/balances"),
        call("GET", "/wallets/"),
        call("GET", "/withdrawals/"),
        call("GET", "/provider/ledger?limit=25"),
      ]);
      setBal(b);
      setWallets(w);
      setWds(x);
      setLedger(l);
      const def = w.find((v) => v.is_default && v.payouts_supported);
      if (def) setWalletId(def.wallet_id);
    } catch (e) {
      setErr(String(e.message || e));
    }
  }, [authHeaders, call]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const verifyWallet = async () => {
    setErr("");
    setMsg("");
    try {
      if (typeof window === "undefined" || !window.ethereum) throw new Error("No browser wallet found (window.ethereum).");
      const [acct] = await window.ethereum.request({ method: "eth_requestAccounts" });
      const addr = address || acct;
      const ch = await call("POST", "/wallets/challenge", { address: addr, network });
      const signature = await window.ethereum.request({ method: "personal_sign", params: [ch.message, addr] });
      await call("POST", "/wallets/verify", { challenge_id: ch.challenge_id, signature });
      setMsg(`Verified ${ch.address} on ${ch.network}`);
      load();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  const withdraw = async () => {
    setErr("");
    setMsg("");
    try {
      const w = await call("POST", "/withdrawals/", { wallet_id: walletId, amount, book: "TEST" }, { "Idempotency-Key": newKey() });
      setMsg(`Withdrawal ${w.withdrawal_id.slice(0, 8)}: ${w.status}, eligible ${w.eligible_at}`);
      setAmount("");
      load();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  const cancel = async (id) => {
    try {
      await call("POST", `/withdrawals/${id}/cancel`);
      load();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  if (!authHeaders || !bal) return err ? <p className="error">{err}</p> : null;
  const b = bal.balances || {};
  return (
    <div className="grid">
      <Section title="Provider balances" action={bal.payout_hold ? <Badge tone="bad">payout hold</Badge> : null}>
        {["TEST", "REAL"].map((book) => (
          <div key={book}>
            <h3>
              {book} <Badge tone={book === "TEST" ? "warn" : "neutral"}>{b[book]?.asset}</Badge>{" "}
              {book === "TEST" ? <span className="muted">simulated — not real money</span> : null}
            </h3>
            <div className="row stats">
              <div><strong>{amt(b[book]?.pending)}</strong><span>pending</span></div>
              <div><strong>{amt(b[book]?.available)}</strong><span>available</span></div>
              <div><strong>{amt(b[book]?.held)}</strong><span>held</span></div>
              <div><strong>{amt(b[book]?.lifetime_paid)}</strong><span>paid out</span></div>
            </div>
          </div>
        ))}
        {msg ? <p className="muted">{msg}</p> : null}
        {err ? <p className="error">{err}</p> : null}
      </Section>

      <Section title="Payout wallets">
        <p className="muted">Sign a message in your wallet to prove ownership. This never authorizes a transfer. Never share a seed phrase.</p>
        <div className="row">
          <input placeholder="0x… (defaults to connected account)" value={address} onChange={(e) => setAddress(e.target.value)} />
          <select value={network} onChange={(e) => setNetwork(e.target.value)}>
            <option value="base-sepolia">Base Sepolia (testnet, payouts)</option>
            <option value="base">Base (ownership only)</option>
          </select>
          <button onClick={verifyWallet}>Verify wallet</button>
        </div>
        <table>
          <thead><tr><th>Address</th><th>Network</th><th>Status</th></tr></thead>
          <tbody>
            {wallets.map((w) => (
              <tr key={w.wallet_id}>
                <td className="mono">{w.address}</td>
                <td>{w.network}</td>
                <td>
                  {w.is_verified ? <Badge tone="good">verified</Badge> : <Badge>unverified</Badge>}{" "}
                  {w.is_default ? <Badge>default</Badge> : null} {w.in_cooldown ? <Badge tone="warn">cooldown</Badge> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="Withdrawals (TEST book)">
        <div className="row">
          <select value={walletId} onChange={(e) => setWalletId(e.target.value)}>
            <option value="">select wallet</option>
            {wallets.filter((w) => w.is_verified && w.payouts_supported).map((w) => (
              <option key={w.wallet_id} value={w.wallet_id}>{w.address.slice(0, 10)}… ({w.network})</option>
            ))}
          </select>
          <input placeholder="amount" value={amount} onChange={(e) => setAmount(e.target.value)} />
          <button disabled={!walletId || !amount} onClick={withdraw}>Request</button>
        </div>
        <table>
          <thead><tr><th>Amount</th><th>Status</th><th>Eligible</th><th>Tx</th><th /></tr></thead>
          <tbody>
            {wds.map((w) => (
              <tr key={w.withdrawal_id}>
                <td>{amt(w.amount, 2)}</td>
                <td><Badge>{w.status}</Badge></td>
                <td className="muted">{w.eligible_at}</td>
                <td className="mono">{w.tx_hash ? `${w.tx_hash.slice(0, 12)}… (sim)` : "—"}</td>
                <td>{["SECURITY_HOLD", "RISK_REVIEW", "APPROVED", "REQUESTED"].includes(w.status) && !w.payout_id ? <button onClick={() => cancel(w.withdrawal_id)}>Cancel</button> : null}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="My ledger">
        <table>
          <thead><tr><th>Type</th><th>Account</th><th>Dr/Cr</th><th>Amount</th><th>When</th></tr></thead>
          <tbody>
            {ledger.map((e, i) => (
              <tr key={i}>
                <td>{e.tx_type}</td>
                <td>{e.code} {e.account}</td>
                <td>{e.direction}</td>
                <td>{amt(e.amount)}</td>
                <td className="muted">{e.posted_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
    </div>
  );
}

export function AdminTreasury({ apiUrl, authHeaders, refreshKey, Section, Badge }) {
  const call = useApi(apiUrl, authHeaders);
  const [sum, setSum] = useState(null);
  const [queue, setQueue] = useState([]);
  const [payouts, setPayouts] = useState([]);
  const [risks, setRisks] = useState([]);
  const [recon, setRecon] = useState(null);
  const [out, setOut] = useState("");
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    if (!authHeaders) return;
    setErr("");
    try {
      const [s, q, p, r, rr] = await Promise.all([
        call("GET", "/admin/finance/treasury/summary?book=TEST"),
        call("GET", "/admin/finance/withdrawals?limit=50"),
        call("GET", "/admin/finance/payouts?limit=25"),
        call("GET", "/admin/finance/risk-events"),
        call("GET", "/admin/finance/reconciliation-runs?limit=1"),
      ]);
      setSum(s);
      setQueue(q.filter((w) => ["SECURITY_HOLD", "RISK_REVIEW", "APPROVED", "BROADCAST", "CONFIRMING"].includes(w.status)));
      setPayouts(p);
      setRisks(r);
      setRecon(rr[0] || null);
    } catch (e) {
      setErr(String(e.message || e));
    }
  }, [authHeaders, call]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const act = async (label, fn) => {
    setErr("");
    try {
      const r = await fn();
      setOut(`${label}: ${JSON.stringify(r).slice(0, 300)}`);
      load();
    } catch (e) {
      setErr(`${label}: ${e.message || e}`);
    }
  };

  if (!authHeaders || !sum) return err ? <p className="error">{err}</p> : null;
  const ks = sum.kill_switches || {};
  return (
    <div className="grid">
      <Section title="Treasury (TEST book, simulated)" action={<Badge tone="warn">no real funds</Badge>}>
        <div className="row stats">
          <div><strong>{amt(sum.total_assets, 4)}</strong><span>assets</span></div>
          <div><strong>{amt(sum.provider_liabilities?.total, 4)}</strong><span>provider liabilities</span></div>
          <div><strong>{amt(sum.customer_prepaid, 4)}</strong><span>customer prepaid</span></div>
          <div><strong>{amt(sum.available_company_funds, 4)}</strong><span>available company funds</span></div>
          <div><strong>{amt(sum.simulated_hot_wallet, 4)}</strong><span>sim hot wallet</span></div>
        </div>
        <p className="muted">{sum.note}</p>
        <p>
          Last reconciliation:{" "}
          {recon ? <Badge tone={recon.status === "OK" ? "good" : "bad"}>{recon.status}</Badge> : "never"}{" "}
          <span className="muted">{recon?.created_at}</span>
        </p>
        <div className="row">
          <button onClick={() => act("confirm revenue", () => call("POST", "/admin/finance/revenue/confirm", {}))}>Confirm revenue (sim)</button>
          <button onClick={() => act("release pending", () => call("POST", "/admin/finance/revenue/release-pending"))}>Release pending</button>
          <button onClick={() => act("payout batch", () => call("POST", "/admin/finance/payouts/run-batch"))}>Run payout batch</button>
          <button onClick={() => act("poll", () => call("POST", "/admin/finance/payouts/poll"))}>Poll payouts</button>
          <button onClick={() => act("reconcile", () => call("POST", "/admin/finance/reconcile?book=TEST"))}>Reconcile</button>
          <button onClick={() => act("daily close", () => call("POST", "/admin/finance/daily-close", { date: new Date(Date.now() - 86400000).toISOString().slice(0, 10) }))}>Close yesterday</button>
        </div>
        <div className="row">
          {Object.entries(ks).map(([name, v]) => (
            <button key={name} onClick={() => act(name, () => call("POST", "/admin/finance/kill-switches", { name, enabled: !v.enabled, reason: "dashboard" }))}>
              {name}: {v.enabled ? "ON (paused)" : "off"}
            </button>
          ))}
        </div>
        {out ? <p className="muted mono">{out}</p> : null}
        {err ? <p className="error">{err}</p> : null}
      </Section>

      <Section title="Withdrawal approval queue">
        <table>
          <thead><tr><th>Amount</th><th>Status</th><th>Risk</th><th>Approvals</th><th>Eligible</th><th /></tr></thead>
          <tbody>
            {queue.map((w) => (
              <tr key={w.withdrawal_id}>
                <td>{amt(w.amount, 2)}</td>
                <td><Badge>{w.status}</Badge></td>
                <td title={(w.risk_reasons || []).join("; ")}>{w.risk_decision} ({w.risk_score})</td>
                <td>{(w.approvals || []).length}/{w.required_approvals}</td>
                <td className="muted">{w.eligible_at}</td>
                <td>
                  {["SECURITY_HOLD", "RISK_REVIEW"].includes(w.status) ? (
                    <>
                      <button onClick={() => act("approve", () => call("POST", `/admin/finance/withdrawals/${w.withdrawal_id}/approve`, {}))}>Approve</button>
                      <button onClick={() => act("reject", () => call("POST", `/admin/finance/withdrawals/${w.withdrawal_id}/reject`, { reason: "rejected in dashboard" }))}>Reject</button>
                    </>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="Payouts (simulated Base Sepolia)">
        <table>
          <thead><tr><th>Amount (atomic)</th><th>Status</th><th>Conf.</th><th>Tx</th></tr></thead>
          <tbody>
            {payouts.map((p) => (
              <tr key={p.payout_id}>
                <td>{p.amount_atomic}</td>
                <td><Badge>{p.status}</Badge></td>
                <td>{p.confirmations}</td>
                <td className="mono">{p.tx_hash ? `${p.tx_hash.slice(0, 14)}…` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="Open risk events">
        <table>
          <thead><tr><th>Category</th><th>Severity</th><th>Message</th><th /></tr></thead>
          <tbody>
            {risks.map((r) => (
              <tr key={r.risk_event_id}>
                <td>{r.category}</td>
                <td><Badge tone={r.severity === "HIGH" ? "bad" : "warn"}>{r.severity}</Badge></td>
                <td>{r.message}</td>
                <td><button onClick={() => act("resolve", () => call("POST", `/admin/finance/risk-events/${r.risk_event_id}/resolve`, { reason: "resolved in dashboard" }))}>Resolve</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
    </div>
  );
}
