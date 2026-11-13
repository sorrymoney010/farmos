"use client";
import { useState } from "react";
const api = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
export default function CryptoFleet() {
  const [token, setToken] = useState("");
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [offset, setOffset] = useState(0);
  async function load(next = 0) {
    setBusy(true); setError(""); setData(null);
    try {
      const response = await fetch(`${api}/api/v1/acurast/fleet-readiness?offset=${next}`, {
        headers: { Authorization: `Bearer ${token}` }, cache: "no-store"
      });
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403
        ? "An owner, admin or operations token is required." : "Could not load phone readiness.");
      setData(await response.json()); setOffset(next);
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  return <main style={{maxWidth: 1000, margin: "40px auto", padding: 24}}>
    <a href="/">← FARMOS dashboard</a>
    <h1>FARMOS crypto phone fleet</h1>
    <p>Connect phones. Check device health. Verify crypto rewards.</p>
    <section className="panel" style={{padding: 20}}>
      <h2>Rewards: verification not connected</h2>
      <p>Confirmed earnings are unknown. Online phones, test credits and estimated rewards are not received crypto.</p>
      <p>This pilot view reads the existing Acurast connection. FARMOS’s own paid job network remains under development.</p>
    </section>
    <label>Fleet access token <input type="password" value={token} autoComplete="off"
      onChange={e => {setToken(e.target.value); setData(null);}}
      placeholder="Owner / admin / operations token" /></label>
    <button disabled={busy || !token} onClick={() => load(0)}>{busy ? "Checking…" : "Check phones"}</button>
    {error && <p role="alert">{error}</p>}
    {data && <>
      <p>{data.telemetry_ready_in_page} phones pass telemetry checks on this page. Network eligibility is still unverified.</p>
      {data.items.length === 0 && <p>No processors registered. Register a processor through FARMOS’s Acurast onboarding endpoint, then connect its signed management check-in.</p>}
      {data.items.map(p => <section className="panel" key={p.processor_id} style={{padding: 20, marginTop: 16}}>
        <h2>{p.label || "Unnamed phone"}</h2>
        <p style={{overflowWrap: "anywhere"}}>{p.address}</p>
        <p>{p.telemetry_ready ? "Telemetry ready" : "Needs attention"}</p>
        <p>Battery: {p.battery_pct ?? "Unknown"}% · Temperature: {p.temperature_c ?? "Unknown"}°C · Power: {p.charging ? "Charging" : "Not confirmed"}</p>
        <p>Last check-in: {p.last_check_in || "Never"}</p>
        {p.blockers.length > 0 && <ul>{p.blockers.map(b => <li key={b}>{b.replaceAll("_", " ")}</li>)}</ul>}
        <p>Crypto rewards: unverified · Network eligibility: unverified</p>
      </section>)}
      <button disabled={busy || offset === 0} onClick={() => load(Math.max(0, offset - 100))}>Previous</button>
      <button disabled={busy || !data.has_more} onClick={() => load(data.next_offset)}>Next</button>
    </>}
  </main>;
}
