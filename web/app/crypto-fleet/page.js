"use client";
import { useState } from "react";
const api = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
export default function CryptoFleet() {
  const [token, setToken] = useState("");
  const [devices, setDevices] = useState([]);
  const [selected, setSelected] = useState("");
  const [claim, setClaim] = useState(null);
  const [payload, setPayload] = useState(JSON.stringify({document: [{name:"FARMOS", units:2}, {name:"Phone", units:3}], ops:[{op:"sum", field:"units"}]}, null, 2));
  const [job, setJob] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function request(path, body) {
    const response = await fetch(`${api}/api/v1${path}`, {method: body === undefined ? "GET" : "POST",
      headers: {Authorization:`Bearer ${token}`, "Content-Type":"application/json"},
      body: body === undefined ? undefined : JSON.stringify(body), cache:"no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`);
    return data;
  }
  async function action(fn) { setBusy(true); setError(""); try {await fn();} catch(e) {setError(e.message);} finally {setBusy(false);} }
  async function refresh() { const data = await request("/admin/fleet"); setDevices(data.devices); }
  async function run() {
    if (new TextEncoder().encode(payload).length > 65536) throw new Error("Keep this pilot JSON under 64 KiB.");
    const parsed = JSON.parse(payload);
    setResult(null);
    const created = await request("/jobs/", {workload_type:"json_transform", payload:parsed, pilot_mode:true, max_retries:0, max_runtime_seconds:60});
    setJob(created);
    const assigned = await request("/jobs/dispatch", {job_id:created.job_id, device_id:selected});
    setJob(assigned);
  }
  async function check() {
    const current = await request(`/jobs/${job.job_id}`); setJob(current);
    if (current.verification?.status === "PASSED") setResult(await request(`/jobs/${job.job_id}/verified-result`));
  }
  return <main style={{maxWidth:1000, margin:"40px auto", padding:24}}>
    <a href="/">← FARMOS dashboard</a><h1>Your FARMOS phone network</h1>
    <p>Use the FARMOS Node Android app. Connect directly to your FARMOS server—no Acurast account or app required.</p>
    <p><strong>Device pilot · no crypto payment attached.</strong> Completed test work is not income.</p>
    <label>Owner/admin access token <input type="password" autoComplete="off" value={token}
      onChange={e=>{setToken(e.target.value);setDevices([]);setJob(null);setResult(null);setClaim(null);setSelected("");}} /></label>
    {error && <p role="alert">{error}</p>}
    <section className="panel" style={{padding:20, marginTop:20}}><h2>1. Pair your phone</h2>
      <p>Install the FARMOS Node APK after its Android build has been validated. Enter this deployed server origin in the app: <code>{api}</code>.</p>
      <p>If this shows localhost, configure a reachable HTTPS backend first. A working APK and server are required; this page does not deploy them.</p>
      <button disabled={busy||!token} onClick={()=>action(async()=>setClaim(await request("/devices/claim-code", {})))}>Create pairing code</button>
      {claim && <p>Code: <strong>{claim.claim_code}</strong> · expires {claim.expires_at}. Enter it in the FARMOS app and tap Enroll.</p>}
    </section>
    <section className="panel" style={{padding:20, marginTop:20}}><h2>2. Choose a native FARMOS phone</h2>
      <button disabled={busy||!token} onClick={()=>action(refresh)}>Refresh phones</button>
      <select value={selected} onChange={e=>setSelected(e.target.value)}><option value="">Choose phone</option>
        {devices.map(d=><option key={d.device_id} value={d.device_id}>{d.human_id} — {d.status}</option>)}</select>
      {devices.map(d=><p key={d.device_id}>{d.human_id}: {d.status} · battery {d.battery_pct??"unknown"}% · {d.temperature_c??"unknown"}°C · last seen {d.last_seen_at||"never"}{d.safety_paused ? ` · paused: ${d.safety_reason}` : ""}</p>)}
      <p>The phone must finish benchmarking and send fresh, safe telemetry. Keep the app open for this pilot.</p>
    </section>
    <section className="panel" style={{padding:20, marginTop:20}}><h2>3. Run your own JSON data job</h2>
      <p>Edit document and ops below. Supported operations: select, filter_eq, sort_by, count, sum. Data is sent to your selected phone.</p>
      <textarea aria-label="JSON job payload" rows={12} style={{width:"100%"}} value={payload} onChange={e=>setPayload(e.target.value)} />
      <button disabled={busy||!token||!selected} onClick={()=>action(run)}>Run unfunded pilot job</button>
      {job && <><p>Job {job.job_id}: {job.status}</p><button disabled={busy} onClick={()=>action(check)}>Check verified result</button></>}
      {result && <pre style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{JSON.stringify(result,null,2)}</pre>}
    </section>
    <p>Next earning milestone: a crypto-funded FARMOS job with a verified receipt. Test jobs and estimates do not count.</p>
  </main>;
}
