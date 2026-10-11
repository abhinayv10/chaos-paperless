"use strict";
// Paperless Chaos Lab dashboard. Two pages: Overview (the website, live) and
// Scan & inject (the campaign, with the self-healing agent and the findings).
// Every value shown comes from the controller API; when data is missing or the
// backend is unreachable we say so.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "–");
const hm = (t) => (t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–");
const fmtMs = (v) => (v == null ? "–" : v >= 1000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms");
const fmtPct = (v) => (v == null ? "–" : (v * 100).toFixed(v > 0 && v < 0.1 ? 1 : 0) + "%");
const fmtBytes = (b) => (b == null ? "–" : b >= 2 ** 30 ? (b / 2 ** 30).toFixed(2) + " GB" : Math.round(b / 2 ** 20) + " MB");
const secs = (v) => (v == null ? "–" : `${v.toFixed(1)} s`);
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const PROBES = ["web_root", "api_documents", "api_status", "doc_upload"];
const WORKFLOWS = { web_root: "Open the web UI", api_documents: "List documents", api_status: "System status check", doc_upload: "Upload a document", doc_processing: "Process a document" };
const SERVICE = { db: "database", broker: "message broker", webserver: "web application", host: "users" };
const PROBE_VAR = { web_root: "--s1", api_documents: "--s2", api_status: "--s3", doc_upload: "--s4" };
const svc = (id) => SERVICE[id] || id;

let currentTab = "overview", safetyState = null, monitor = null, lastView = null, scoreData = null, aiInfo = null;

function humanize(text) {
  return String(text ?? "")
    .replace(/\b(web_root|api_documents|api_status|doc_upload|doc_processing)\b/g, (m) => `"${WORKFLOWS[m]}"`)
    .replace(/\bdb\b/g, "database").replace(/(?<!message )\bbroker\b/gi, "message broker")
    .replace(/\b(the|a|an) the /gi, "the ").replace(/ exp-\d{8}-\d{6}-[0-9a-f]{4}/g, "");
}
const faultName = (c) => ({ dependency_pause: "freeze (pause)", dependency_stop: "crash (stop)", network_latency: `${c.latency_ms ? "+" + c.latency_ms + " ms " : ""}latency`,
  packet_loss: `${c.loss_pct ?? ""}% packet loss`, cpu_exhaustion: `CPU capped at ${c.cpu_limit ?? "?"}`,
  connection_refused: "connections refused", memory_exhaustion: `memory squeezed to ${c.memory_mb ?? "?"} MB`,
  disk_exhaustion: "upload disk full" }[c.fault_type] || c.fault_type);
const faultTitle = (c) => (c && c.target ? (c.fault_type === "disk_exhaustion" ? "Upload disk full" : `${cap(svc(c.target))} ${faultName(c)}`) : "Experiment");
const faultIs = (c) => ({ dependency_pause: `The ${svc(c.target)} is frozen`, dependency_stop: `The ${svc(c.target)} is stopped`,
  network_latency: `The ${svc(c.target)} answers ${c.latency_ms} ms slower`, packet_loss: `The ${svc(c.target)} is losing ${c.loss_pct}% of its packets`,
  cpu_exhaustion: `The ${svc(c.target)} is capped at ${c.cpu_limit} CPU`, connection_refused: `Every connection to the ${svc(c.target)} is refused`,
  memory_exhaustion: `The ${svc(c.target)} is squeezed into ${c.memory_mb} MB of memory`, disk_exhaustion: "The upload disk is full" }[c.fault_type] || `The ${svc(c.target)} is disturbed`);

// ---------------------------------------------------------------- utilities
async function api(path, opts = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...opts };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  let res;
  try { res = await fetch(path, init); } catch (e) { setPill(false); throw new Error("controller unreachable"); }
  const text = await res.text();
  let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const err = new Error(describeError(data) || res.statusText);
    err.status = res.status; err.data = data; throw err;
  }
  return data;
}
function describeError(d) {
  if (!d) return "";
  if (Array.isArray(d.detail)) return d.detail.map((x) => (typeof x === "string" ? x : x.msg)).join("; ");
  if (d.detail) return String(d.detail);
  if (d.errors) return d.errors.join("; ");
  return typeof d === "string" ? d : "";
}
// One status pill for the whole app: connection, active fault, AI model.
function setPill(ok) {
  const el = $("#state-pill");
  if (!ok) { el.innerHTML = chip("Controller unreachable", "critical"); return; }
  const fault = safetyState && safetyState.active_fault;
  const ai = aiInfo && aiInfo.provider ? `AI: ${aiInfo.model}` : "AI: rules only";
  el.innerHTML = fault ? chip(`Fault active on the ${svc(fault.service)} · ${ai}`, "serious") : chip(`Connected · ${ai}`, aiInfo && aiInfo.provider ? "good" : "warning");
}
let toastTimer;
function toast(msg, ms = 5000) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), ms);
}
// Status chip: a coloured dot plus a label in normal ink (never colour alone).
const chip = (label, tone) => `<span class="status st-${tone || "unknown"}">${esc(label)}</span>`;
function toneOf(s) {
  s = String(s || "").toLowerCase();
  if (["healthy", "running", "ok", "completed", "external", "recovered"].includes(s)) return "good";
  if (["starting", "restarting"].includes(s)) return "warning";
  if (["unhealthy"].includes(s)) return "serious";
  if (["paused", "exited", "dead", "missing"].includes(s)) return "critical";
  return "unknown";
}
const SEV_TONE = { critical: "critical", high: "serious", medium: "warning", low: "good", none: "good", inconclusive: "unknown" };
function rows(tbody, html, cols, emptyMsg) {
  tbody.innerHTML = html.length ? html.join("") : `<tr><td colspan="${cols}" class="muted">${esc(emptyMsg)}</td></tr>`;
}
function pctl(values, p) {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  return s[Math.max(0, Math.min(s.length - 1, Math.ceil((p / 100) * s.length) - 1))];
}
function since(iso) {
  const t = Date.parse(iso); if (!t) return "–";
  const s = Math.max(0, (Date.now() - t) / 1000);
  return s < 120 ? `${Math.round(s)} s` : s < 7200 ? `${Math.round(s / 60)} m` : `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} m`;
}

// ---------------------------------------------------------------- tabs, kill switch, unblock
$$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
document.addEventListener("click", (e) => { const b = e.target.closest("[data-go]"); if (b) showTab(b.dataset.go); });
function showTab(name) {
  currentTab = name;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  history.replaceState(null, "", "#" + name);
  refreshAll();
}
$("#kill").addEventListener("click", async () => {
  $("#kill").disabled = true;
  try {
    const r = await api("/api/kill", { method: "POST" });
    toast((r.rollback_verified ? "Kill switch: all faults removed (verified). " : "Kill switch: rollback NOT verified! ") + (r.notes || []).join("; "), 8000);
  } catch (e) { toast("Kill switch failed: " + e.message + ". Run `python -m chaos kill` in a terminal.", 12000); }
  finally { $("#kill").disabled = false; refreshAll(); }
});
$("#unblock").addEventListener("click", async () => {
  try {
    const r = await api("/api/safety/unblock", { method: "POST" });
    toast(r.ok ? "Environment verified; experiments unblocked." : "Still blocked: " + r.checks.filter((c) => !c.ok).map((c) => `${c.name} (${c.detail})`).join("; "), 9000);
  } catch (e) { toast("Unblock failed: " + e.message); }
  refreshAll();
});

// ---------------------------------------------------------------- the experiment on the Overview
// The live run if there is one, otherwise the most recent recorded experiment.
let lastRunCache = { key: null, run: null };
async function getView() {
  const d = await api("/api/experiments/current");
  const live = d.live || {};
  if (live.run_id && live.kind === "experiment") {
    return { busy: d.busy, source: "live", run_id: live.run_id, state: live.state, config: live.config || {},
      fault_start: live.fault_start, fault_end: live.fault_end, samples: live.samples || [], events: live.events || [] };
  }
  if (d.busy) return { busy: true, source: "other", state: live.state || "running", config: live.config || {}, samples: [], events: [] };
  const runs = await api("/api/runs?kind=experiment&limit=10");
  const head = runs.find((r) => r.state !== "rejected");
  if (!head) return null;
  const key = head.id + head.state + head.ended;
  if (lastRunCache.key !== key) lastRunCache = { key, run: await api("/api/runs/" + encodeURIComponent(head.id)) };
  const r = lastRunCache.run, o = r.outcome || {}, rec = o.recovery || {};
  return { busy: false, source: "last", run_id: r.id, state: r.state, config: r.config || {}, fault_start: r.fault_start, fault_end: r.fault_end,
    samples: r.samples || [], events: r.events || [], recoveredAt: rec.recovery_time_s != null && r.fault_end ? r.fault_end + rec.recovery_time_s : null };
}
function phaseStats(samples, probes, phases) {
  const r = samples.filter((s) => probes.includes(s.probe) && phases.includes(s.phase));
  if (!r.length) return null;
  return { n: r.length, err: r.filter((s) => !s.ok).length / r.length, p95: pctl(r.map((s) => s.latency_ms || 0), 95) };
}
function verdict(st, base) {
  if (!st) return null;
  if (st.err >= 0.5) return ["Failing", "critical"];
  if (st.err > 0) return ["Some errors", "warning"];
  if (base && base.p95 && st.p95 > 2 * base.p95 && st.p95 - base.p95 > 200) return ["Slower", "warning"];
  return ["OK", "good"];
}
function renderWorkflows(el, samples) {
  if (!samples || !samples.length) { el.innerHTML = `<div class="empty">No measurements yet.</div>`; return; }
  const c = (vd, st) => (vd ? `<td title="${fmtPct(st.err)} of requests failed · response time ${fmtMs(st.p95)}">${chip(vd[0], vd[1])} <span class="small muted">${st.err > 0 ? fmtPct(st.err) + " errors" : fmtMs(st.p95)}</span></td>` : `<td class="muted">–</td>`);
  el.innerHTML = `<table class="tbl wf"><thead><tr><th>Workflow</th><th>Before</th><th>During the fault</th><th>After</th></tr></thead><tbody>`
    + Object.entries(WORKFLOWS).map(([p, label]) => {
      const base = phaseStats(samples, [p], ["baseline"]), fault = phaseStats(samples, [p], ["fault"]), after = phaseStats(samples, [p], ["recovery", "post"]);
      return `<tr><td><b>${esc(label)}</b></td>${c(verdict(base), base)}${c(verdict(fault, base), fault)}${c(verdict(after, base), after)}</tr>`;
    }).join("") + `</tbody></table>`;
}

// ---------------------------------------------------------------- overview: status, metrics, resources
const LOG = {
  preflight_passed: ["Preflight passed", "good"], fault_injected: ["Fault injected", "serious"],
  threshold_breach: ["Automatic abort", "critical"], manual_abort: ["Aborted by kill switch", "warning"], kill_switch: ["Kill switch", "warning"],
  fault_removed: ["Fault removed (verified)", "good"], rollback_failed: ["Rollback FAILED", "critical"], recovery_verified: ["Recovery verified", "good"],
  recovery_failed: ["Recovery NOT verified", "critical"], blocked: ["Experiments blocked", "critical"], unblocked: ["Unblocked", "warning"],
  finished: ["Run finished", "unknown"], analysis_done: ["Analysed", "unknown"], aborted: ["Aborted", "warning"], rejected: ["Rejected by safety", "warning"],
  kill_rollback: ["Kill-switch rollback", "warning"], controller_error: ["Controller error", "critical"], fault_error: ["Fault error", "critical"],
  agent_watching: ["Agent watching", "unknown"], agent_remediated: ["Agent fixed it", "good"], agent_fix_ineffective: ["Agent fix ineffective", "warning"],
  threshold_breach_observed: ["Threshold breached (held for agent)", "warning"], agent_error: ["Agent error", "critical"],
};
function renderLog(v) {
  const events = v && v.events.length ? v.events : (safetyState ? safetyState.events : []);
  const out = events.filter((e) => LOG[e.kind]).sort((a, b) => b.ts - a.ts);
  rows($("#ov-log tbody"), out.slice(0, 20).map((e) => `<tr><td class="muted nowrap">${fmtTime(e.ts)}</td><td class="nowrap">${chip(...LOG[e.kind])}</td><td class="small">${esc(e.message)}</td></tr>`), 3, "no events yet");
}
async function loadOverview() {
  let d;
  try { d = await api("/api/overview"); } catch (e) { $("#ov-status").innerHTML = chip("Controller unreachable", "critical"); $("#ov-text").textContent = e.message; return; }
  monitor = d.monitor; safetyState = d.safety;
  setPill(true);
  $("#blocked-banner").hidden = !d.safety.blocked;
  if (d.safety.blocked) $("#blocked-reason").textContent = `${d.safety.blocked.reason} (${fmtTime(d.safety.blocked.ts)}). No experiment can start until the environment is verified.`;
}
// The headline: application health, or what the running experiment is doing to it.
function renderHealth(v) {
  const m = monitor || {}, svcs = m.services || [];
  let st, text;
  if (safetyState && safetyState.blocked) { st = ["Experiments blocked", "critical"]; text = safetyState.blocked.reason; }
  else if (m.docker_error) { st = ["Docker unreachable", "critical"]; text = m.docker_error; }
  else if (v && v.source === "live" && ["injecting", "active"].includes(v.state)) { st = ["Fault active", "serious"]; text = `${faultIs(v.config)}. ${v.config.remediation === "agent" ? "The self-healing agent has to find and fix it." : ""}`; }
  else if (v && v.source === "live") { st = ["Experiment running", "warning"]; text = `${faultTitle(v.config)}: ${String(v.state || "").replace(/_/g, " ")}.`; }
  else if (!m.ts) { st = ["Waiting for the first check", "unknown"]; text = "The monitor checks the application every 5 s."; }
  else if (!m.app_ok) { st = ["Application degraded", "critical"]; text = (m.probes || []).filter((p) => !p.ok).map((p) => `${WORKFLOWS[p.probe] || p.probe}: ${p.detail}`).join("; "); }
  else { st = ["All systems healthy", "good"]; text = `Paperless-ngx is answering normally. No fault is active. Last checked ${fmtTime(m.ts)}, every 5 s.`; }
  $("#ov-status").innerHTML = chip(st[0], st[1]);
  $("#ov-text").textContent = text;
  const order = ["webserver", "db", "broker"];
  const comps = order.map((id) => svcs.find((s) => s.service === id)).filter(Boolean).map((s) => {
    const state = s.state === "running" ? (s.health === "none" ? "running" : s.health) : s.state;
    return `<li>${chip(cap(svc(s.service)), toneOf(state))}<span class="small muted">${s.state === "running" ? "up " + since(s.started_at) : esc(state)}</span></li>`;
  });
  if (metricsData && metricsData.disk) comps.push(`<li>${chip("Upload disk", metricsData.disk.used_pct >= 90 ? "critical" : "good")}<span class="small muted">${metricsData.disk.used_pct}% of ${metricsData.disk.total_mb} MB</span></li>`);
  $("#ov-comps").innerHTML = comps.join("") || `<li class="muted small">no containers found</li>`;
}
function renderScore() {
  if (!scoreData) return;
  const o = scoreData.overall;
  $("#ov-score").innerHTML = o.score == null ? `<span class="muted">n/a</span>` : `${Math.round(o.score)}<small> / 100</small>`;
  $("#ov-cov").textContent = o.score == null ? "no experiment scored yet" : `${o.scenarios_scored} of ${o.scenarios_total} failure scenarios tested`;
}
let metricsData = null;
function spark(values, colorVar) {
  const v = (values || []).map((x) => +x || 0);
  if (v.length < 2) return `<div class="spark empty-spark"></div>`;
  const w = 160, h = 30, lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
  const pts = v.map((y, i) => [i / (v.length - 1) * (w - 4) + 2, h - 4 - (y - lo) / span * (h - 8)]);
  const line = pts.map((p) => p.map((n) => n.toFixed(1)).join(",")).join(" "), last = pts[pts.length - 1], c = `var(${colorVar})`;
  return `<div class="spark"><svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><polygon points="2,${h - 2} ${line} ${w - 2},${h - 2}" fill="${c}" opacity=".12"/><polyline points="${line}" fill="none" stroke="${c}" stroke-width="1.6"/><circle cx="${last[0]}" cy="${last[1]}" r="2.6" fill="${c}"/></svg></div>`;
}
async function loadMetrics() {
  try { metricsData = await api("/api/metrics"); } catch { return; }
  const m = metricsData, rq = m.requests, t = m.trend;
  const tile = (k, v, unit, trend, color, note) => `<div class="metric"><div class="k">${k}</div><div class="v">${v}${unit ? `<small>${unit}</small>` : ""}</div>${spark(trend, color)}<div class="small muted">${note}</div></div>`;
  const dc = m.documents.change;
  $("#ov-metrics").innerHTML =
    tile("Requests answered", rq.per_min ?? "–", "/min", t.rpm, "--s1", "lab traffic, last 15 min")
    + tile("Error rate", rq.error_rate == null ? "–" : (rq.error_rate * 100).toFixed(1), "%", t.error_rate, rq.error_rate ? "--critical" : "--good", `${rq.failed} failed of ${rq.total}`)
    + tile("Median response", rq.median_ms == null ? "–" : (rq.median_ms / 1000).toFixed(1), " s", t.median_ms, "--s3", `p95 ${fmtMs(rq.p95_ms)} · timeout ${m.client_timeout_s} s`)
    + tile("Documents stored", m.documents.count ?? "–", "", m.documents.trend, "--s2", dc == null ? "count from the Paperless API" : `${dc >= 0 ? "+" : ""}${dc} in the last 15 min`)
    + tile("Upload → processed", m.processing.median_ms == null ? "–" : (m.processing.median_ms / 1000).toFixed(1), " s", t.processing_ms, "--s4", m.processing.count ? `median of the last ${m.processing.count} documents` : "no uploads in the last 6 h")
    + tile("Task queue", m.queue.length ?? "–", "", m.queue.trend, "--s1", "waiting for the worker");
  const res = m.resources || {}, order = ["webserver", "db", "broker"];
  const bar = (pct) => `<span class="mbar"><i style="width:${Math.min(100, Math.max(1, pct))}%"></i></span>`;
  rows($("#ov-res tbody"), order.filter((s) => res[s]).map((s) => {
    const r = res[s];
    const memPct = r.memory_limit ? (r.mem_bytes / r.memory_limit) * 100 : r.mem_total_bytes ? (r.mem_bytes / r.mem_total_bytes) * 100 : 0;
    const limits = [r.cpu_limit ? `${r.cpu_limit} CPU` : "", r.memory_limit ? `${fmtBytes(r.memory_limit)} memory` : ""].filter(Boolean).join(", ") || "none";
    return `<tr><td><b>${esc(cap(svc(s)))}</b></td><td>${bar(r.cpu_pct)} <span class="num small">${r.cpu_pct.toFixed(1)}%</span></td><td>${bar(memPct)} <span class="num small">${fmtBytes(r.mem_bytes)}</span></td><td class="small ${limits === "none" ? "muted" : ""}">${esc(limits)}</td></tr>`;
  }), 4, "Collecting resource use…");
}
async function loadScore() { try { scoreData = await api("/api/findings"); renderScore(); } catch { /* shown as n/a */ } }
async function loadAi() { try { aiInfo = await api("/api/ai/status"); setPill(true); } catch { /* pill shows the problem */ } }
async function loadGraph() {
  let g;
  try { g = await api("/api/graph"); } catch (e) { $("#ov-graph").innerHTML = `<div class="empty">Discovery failed: ${esc(e.message)}</div>`; return; }
  const faultOn = safetyState && safetyState.active_fault ? safetyState.active_fault.service : null;
  const lastOn = !faultOn && lastView && lastView.fault_start ? lastView.config.target : null;
  $("#ov-graph-note").textContent = g.source.runtime_error ? "live status unavailable: Docker is unreachable" : "discovered automatically";
  const ids = g.nodes.map((n) => n.id), inc = {}, depth = {};
  ids.forEach((i) => (inc[i] = []));
  g.edges.forEach((e) => inc[e.to] && inc[e.to].push(e.from));
  const d = (id, seen = new Set()) => {
    if (depth[id] != null) return depth[id];
    if (seen.has(id)) return 0; seen.add(id);
    return (depth[id] = inc[id].length ? 1 + Math.max(...inc[id].map((p) => d(p, seen))) : 0);
  };
  ids.forEach((i) => d(i));
  const layers = {}; g.nodes.forEach((n) => (layers[depth[n.id]] ||= []).push(n));
  const NW = 168, NH = 56, GX = 56, GY = 14;
  const nl = Object.keys(layers).length, maxRows = Math.max(1, ...Object.values(layers).map((l) => l.length));
  const W = nl * NW + (nl - 1) * GX + 8, H = maxRows * NH + (maxRows - 1) * GY + 8, pos = {};
  Object.entries(layers).forEach(([L, ns]) => ns.forEach((n, i) => {
    pos[n.id] = { x: 4 + L * (NW + GX), y: (H - (ns.length * NH + (ns.length - 1) * GY)) / 2 + i * (NH + GY) };
  }));
  const COL = { good: "var(--good)", warning: "var(--warning)", serious: "var(--serious)", critical: "var(--critical)" };
  let s = `<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0L10,5L0,10z" fill="var(--text-2)"/></marker></defs>`;
  for (const e of g.edges) {
    const a = pos[e.from], b = pos[e.to]; if (!a || !b) continue;
    const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
    s += `<path d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 2},${y2}" fill="none" stroke="var(--text-2)" stroke-width="1.5" marker-end="url(#arr)"><title>${esc(e.from)} → ${esc(e.to)}\n${esc(e.evidence.join("\n"))}</title></path>`;
  }
  for (const n of g.nodes) {
    const p = pos[n.id], hit = n.id === faultOn, tone = hit ? "critical" : toneOf(n.status), stroke = COL[tone] || "var(--border)";
    const state = hit ? "✕ fault active now" : n.kind === "external" ? "sends requests" : `${tone === "good" ? "✓" : "!"} ${n.status}` + (n.id === lastOn ? " · last fault here" : "");
    s += `<g><rect x="${p.x}" y="${p.y}" width="${NW}" height="${NH}" rx="8" fill="var(--surface)" stroke="${stroke}" stroke-width="${hit ? 3 : 1.5}" ${n.id === lastOn ? `stroke-dasharray="5 3"` : ""}><title>${esc(n.image || n.note || n.id)}</title></rect>
      <text x="${p.x + 12}" y="${p.y + 23}" font-size="14" font-weight="700" fill="var(--text)">${esc(cap(svc(n.id)))}</text>
      <text x="${p.x + 12}" y="${p.y + 42}" font-size="12" fill="var(--text-2)">${esc(state)}</text></g>`;
  }
  $("#ov-graph").innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="max-height:${Math.round(H * 0.95)}px" role="img" aria-label="dependency map">${s}</svg>`;
}

// ---------------------------------------------------------------- chart
function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}
function timeTicks(t0, t1, n = 6) {
  const span = t1 - t0, steps = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800];
  const step = steps.find((s) => span / s <= n) || 3600;
  const out = []; for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t);
  return out;
}
function bands(sx, opts, top, h) {
  const band = (a, b, fill, label) => {
    if (!a) return "";
    const x0 = sx(a), x1 = sx(b || opts.tEnd);
    return `<rect x="${x0}" y="${top}" width="${Math.max(1, x1 - x0)}" height="${h}" fill="var(${fill})"/><text x="${x0 + 5}" y="${top + 15}" font-size="13" font-weight="600" fill="var(--text-2)">${label}</text>`;
  };
  return band(opts.faultStart, opts.faultEnd, "--fault-band", "fault active") + (opts.faultEnd && opts.recoveredAt ? band(opts.faultEnd, opts.recoveredAt, "--recovery-band", "") : "");
}
function latencyChart(el, samples, opts = {}) {
  const probes = opts.probes || PROBES;
  const data = samples.filter((s) => probes.includes(s.probe));
  if (!data.length) { el.innerHTML = `<div class="empty">No response-time samples for this run.</div>`; return; }
  const W = 960, H = opts.height || 260, m = { l: 58, r: 16, t: 10, b: 30 };
  const t0 = Math.min(...data.map((s) => s.ts), opts.faultStart || Infinity);
  const t1 = Math.max(...data.map((s) => s.ts), opts.faultEnd || 0, opts.now || 0);
  const ymax = niceMax(Math.max(...data.map((s) => s.latency_ms || 0)) * 1.08);
  const sx = (t) => m.l + ((t - t0) / Math.max(1, t1 - t0)) * (W - m.l - m.r);
  const sy = (v) => H - m.b - (v / ymax) * (H - m.t - m.b);
  let g = bands(sx, { ...opts, tEnd: t1 }, m.t, H - m.t - m.b);
  (opts.marks || []).filter((k) => k.t >= t0 && k.t <= t1).forEach((k, i) => {
    const x = sx(k.t);
    g += `<line x1="${x}" x2="${x}" y1="${m.t}" y2="${H - m.b}" stroke="var(--accent)" stroke-width="1.5" stroke-dasharray="4 3"/><text x="${x + 4}" y="${m.t + 32 + i * 15}" font-size="12" font-weight="600" fill="var(--accent)">${esc(k.label)}</text>`;
  });
  for (let i = 0; i <= 4; i++) {
    const v = (ymax / 4) * i, y = sy(v);
    g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y + 4}" text-anchor="end" font-size="12" fill="var(--muted)">${fmtMs(v)}</text>`;
  }
  for (const t of timeTicks(t0, t1)) g += `<text x="${sx(t)}" y="${H - 8}" text-anchor="middle" font-size="12" fill="var(--muted)">${hm(t)}</text>`;
  for (const p of probes) {
    const pts = data.filter((s) => s.probe === p).sort((a, b) => a.ts - b.ts);
    if (!pts.length) continue;
    g += `<polyline fill="none" stroke="var(${PROBE_VAR[p]})" stroke-width="2" stroke-linejoin="round" points="${pts.map((s) => `${sx(s.ts).toFixed(1)},${sy(s.latency_ms || 0).toFixed(1)}`).join(" ")}"/>`;
    for (const s of pts.filter((s) => !s.ok)) {
      const x = sx(s.ts), y = sy(s.latency_ms || 0);
      g += `<path d="M${x - 5},${y - 5}L${x + 5},${y + 5}M${x - 5},${y + 5}L${x + 5},${y - 5}" stroke="var(--critical)" stroke-width="2.5"/>`;
    }
  }
  g += `<line class="xh" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" stroke="var(--muted)" stroke-dasharray="3 3" visibility="hidden"/>`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="response time per workflow over time">${g}<rect x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent" class="hit"/></svg>`;
  const svg = el.querySelector("svg"), xh = svg.querySelector(".xh");
  svg.querySelector(".hit").addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect(), x = ((ev.clientX - r.left) / r.width) * W;
    const t = t0 + ((x - m.l) / (W - m.l - m.r)) * (t1 - t0);
    const near = {};
    for (const s of data) if (!near[s.probe] || Math.abs(s.ts - t) < Math.abs(near[s.probe].ts - t)) near[s.probe] = s;
    xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.setAttribute("visibility", "visible");
    showTip(ev, `${new Date(t * 1000).toLocaleTimeString()}\n` + probes.filter((p) => near[p]).map((p) => `${WORKFLOWS[p]}: ${fmtMs(near[p].latency_ms)} ${near[p].ok ? "ok" : "FAILED " + (near[p].detail || "")}`).join("\n"));
  });
  svg.querySelector(".hit").addEventListener("mouseleave", () => { hideTip(); xh.setAttribute("visibility", "hidden"); });
}
function showTip(ev, text) { const t = $("#tip"); t.textContent = text; t.hidden = false; t.style.left = Math.min(ev.clientX + 14, innerWidth - 380) + "px"; t.style.top = ev.clientY + 14 + "px"; }
function hideTip() { $("#tip").hidden = true; }
function legend(el, agentMarks) {
  el.innerHTML = PROBES.map((p) => `<span><span class="sw" style="background:var(${PROBE_VAR[p]})"></span>${WORKFLOWS[p]}</span>`).join("")
    + `<span><b style="color:var(--critical)">✕</b> failed request</span><span><span class="sw" style="background:var(--fault-band);height:10px"></span>fault active</span>`
    + (agentMarks ? `<span><span class="sw" style="background:var(--accent)"></span>agent: detected / fix applied / verified</span>` : "");
}
function renderOverviewRun(v) {
  const el = $("#chart-ov");
  $("#wf-note").textContent = !v || v.source === "other" ? "" : `${v.source === "live" ? "running now" : "latest experiment"}: ${faultTitle(v.config)}`;
  renderWorkflows($("#wf-ov"), v && v.source !== "other" ? v.samples : null);
  if (!v || v.source === "other" || !v.samples.length) { el.dataset.sig = ""; el.innerHTML = `<div class="empty">The chart appears when an experiment runs.</div>`; return; }
  const sig = [v.run_id, v.samples.length, v.fault_end].join("|");
  if (el.dataset.sig === sig) return;   // unchanged: keep the hover state
  el.dataset.sig = sig;
  latencyChart(el, v.samples, { faultStart: v.fault_start, faultEnd: v.fault_end, recoveredAt: v.recoveredAt, now: v.busy ? Date.now() / 1000 : null, height: 330 });
}

// ---------------------------------------------------------------- campaign
let campChecked = null, campRunCache = { key: null, run: null, inc: null, grade: null };
const CLASS = { db_unavailable: "Database down (not answering)", db_slow: "Database network degraded", db_cpu_starved: "Database starved of CPU",
  broker_unavailable: "Message broker down (not answering)", broker_slow: "Message broker network degraded", broker_cpu_starved: "Message broker starved of CPU",
  db_refusing_connections: "Database refusing connections", broker_refusing_connections: "Message broker refusing connections",
  db_memory_exhausted: "Database out of memory", broker_memory_exhausted: "Message broker out of memory", webserver_disk_full: "Upload disk full", unknown: "Unknown: cannot attribute" };
const HSTATUS = {
  learning: ["learning normal behaviour", "unknown"], watching: ["watching, all normal", "good"], detected: ["failure detected", "critical"],
  diagnosing: ["diagnosing", "serious"], remediating: ["applying the fix", "serious"], verifying: ["verifying the fix", "warning"],
  resolved: ["self-healed", "good"], escalated: ["escalated to a human", "critical"], no_incident: ["no failure detected", "unknown"], interrupted: ["interrupted", "critical"],
};
const CP_NODE = { scan: ["Scan", "live probes"], rank: ["Rank", "AI model"], select: ["Select", "AI + safety policy"], confirm: ["Confirm", "you"], execute: ["Inject & heal", "agent + tools"], report: ["Report", "findings"] };
const CP_STATUS = { scanning: ["Scanning", "warning"], ranking: ["AI is ranking", "serious"], awaiting_confirmation: ["Waiting for your confirmation", "warning"], running: ["Injecting failures", "serious"], done: ["Campaign complete", "good"], cancelled: ["Cancelled", "unknown"], stopped: ["Stopped", "warning"], failed: ["Failed", "critical"] };
const SEV_T = { high: "critical", medium: "warning", low: "good" };
const SEV_RANK = { critical: 4, high: 3, medium: 2, low: 1, none: 0 };
const cpDone = (c) => !c || !!c.ended || ["done", "cancelled", "failed", "stopped"].includes(c.status);
const citeBtn = (id) => `<span class="cite ${id.startsWith("N") ? "n" : ""}" data-cite="${esc(id)}" title="hover to highlight the measurement">${esc(id)}</span>`;
const whoDiag = (src) => (src.mode === "llm" ? `AI model (${src.model})` : src.mode === "rules-fallback" ? "rules (AI unavailable)" : "rules");
document.addEventListener("mouseover", (e) => { const c = e.target.closest(".cite"); if (c) $$(`[data-sig="${c.dataset.cite}"]`).forEach((x) => x.classList.add("hl")); });
document.addEventListener("mouseout", (e) => { if (e.target.closest(".cite")) $$(".hl").forEach((x) => x.classList.remove("hl")); });

function cpHero(c) {
  if (!c) return [["Ready", "good"], "Scan the live application for weak points. The AI ranks them and proposes experiments that test every one; nothing is injected until you confirm."];
  const st = CP_STATUS[c.status] || [c.status, "unknown"];
  const items = (c.selection || {}).items || [];
  const cur = items.find((x) => x.status === "running");
  const last = (c.scan.steps || []).slice(-1)[0];
  const r = c.report;
  return [st, {
    scanning: last ? `${last.title}: ${last.detail}` : "Starting the scan…",
    ranking: `The AI is ranking ${c.scan.findings.length} weak points and choosing experiments that test them.`,
    awaiting_confirmation: `The AI proposed ${items.length} experiments. Review them below and approve at least ${(c.selection || {}).min || 3} to start injecting.`,
    running: cur ? `Injecting ${cur.id}: ${faultTitle(cur)}. The self-healing agent is watching without being told what was injected.` : "Preparing the next experiment…",
    done: r ? `${r.experiments} experiments run; ${r.self_healed} fixed by the self-healing agent with ${r.correct_diagnoses} correct diagnoses; ${r.findings.filter((f) => f.confirmed).length} of ${r.findings.length} tested weak points confirmed.` : "",
    cancelled: "Cancelled before anything was injected.", stopped: "Stopped by the operator after the running experiment finished safely.",
    failed: c.error || "The campaign failed; the engine's safety controls still removed any active fault.",
  }[c.status] || ""];
}
function cpKpis(c) {
  const items = c && c.selection ? c.selection.items : [];
  const done = items.filter((x) => x.result);
  const healed = done.filter((x) => x.result.agent.remediated_by === "agent").length;
  const confirmed = c && c.scan ? c.scan.findings.filter((f) => findingVerdict(c, f).confirmed).length : 0;
  const k = (label, v) => `<div class="kpi"><div class="k">${label}</div><div class="v">${v}</div></div>`;
  $("#cp-kpis").innerHTML = k("Weak points", c && c.scan ? c.scan.findings.length : "–") + k("Approved", c && c.approved ? c.approved.length : "–")
    + k("Self-healed", done.length ? `${healed} / ${done.length}` : "–") + k("Confirmed", c && c.scan && done.length ? `${confirmed} / ${c.scan.findings.length}` : "–");
}
function cpFlow(c) {
  $("#cp-flow").innerHTML = Object.keys(CP_NODE).map((n) => {
    const tr = c ? c.trace.filter((x) => x.node === n) : [];
    let cls = tr.length ? "done" : "todo";
    if (c && !cpDone(c) && (c.node === n || (n === "confirm" && c.status === "awaiting_confirmation"))) cls = "now";
    if (c && c.status === "awaiting_confirmation" && n === "select") cls = "done";
    let d = tr.length ? tr[tr.length - 1].text : "";
    if (n === "confirm" && c && c.status === "awaiting_confirmation") d = "approve at least 3 below";
    return `<li class="${cls}"><div class="t">${esc(CP_NODE[n][0])}</div><div class="who">${esc(CP_NODE[n][1])}</div><div class="d">${esc(d.length > 140 ? d.slice(0, 137) + "…" : d)}</div></li>`;
  }).join("");
}
function cpScan(c) {
  const steps = c ? c.scan.steps || [] : [];
  $("#cp-scan").innerHTML = steps.length ? steps.map((s) => `<li><span class="ic ${s.status}">${s.status === "done" ? "✓" : s.status === "failed" ? "✕" : "…"}</span><div><b>${esc(s.title)}</b><div class="d">${esc(s.detail)}</div></div></li>`).join("")
    : `<li class="muted">${c ? "Starting…" : "Start a scan to probe the live application."}</li>`;
}
function cpFindings(c) {
  const box = $("#cp-findings");
  if (!c || !c.scan.findings.length) { box.innerHTML = `<div class="empty">${c && c.status === "scanning" ? "Scanning…" : "The ranked findings appear after the scan."}</div>`; box.dataset.html = ""; $("#cp-rank-src").innerHTML = ""; return; }
  const byId = Object.fromEntries(c.scan.findings.map((f) => [f.id, f]));
  const order = c.ranking ? c.ranking.order : c.scan.findings.map((f, i) => ({ finding_id: f.id, rank: i + 1, rationale: "" }));
  const src = c.ranking && c.ranking.source;
  $("#cp-rank-src").innerHTML = !src ? `<span class="tag">ranking…</span>` : src.mode === "llm" ? `<span class="tag ai">ranked by AI: ${esc(src.model)}</span>` : `<span class="tag">ranked by rules${src.error ? " (AI unavailable)" : ""}</span>`;
  const html = order.map((o) => { const f = byId[o.finding_id]; return `<div class="finding"><div class="rank">${o.rank}</div><div>
      <div class="t">${esc(f.title)} ${chip(cap(f.severity), SEV_T[f.severity])} <span class="small muted">${esc(f.id)}</span></div>
      ${o.rationale ? `<div class="small">${esc(humanize(o.rationale))}</div>` : ""}
      <details class="small"><summary class="muted">evidence and possible tests</summary><ul>${f.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>
      <div class="muted">Testable by: ${esc(f.tests.map((t) => faultTitle(t)).join(" · "))}</div></details></div></div>`; }).join("");
  if (box.dataset.html !== html) { box.dataset.html = html; box.innerHTML = html; }
}
function cpSelection(c) {
  const box = $("#cp-select"), sel = c && c.selection;
  const awaiting = c && c.status === "awaiting_confirmation";
  $("#cp-confirm").hidden = !awaiting;
  if (!sel) { box.innerHTML = `<div class="empty">The AI proposes experiments after ranking. Nothing is injected until you confirm.</div>`; box.dataset.html = ""; $("#cp-sel-src").innerHTML = ""; $("#cp-sel-note").textContent = ""; $("#cp-confirm-note").textContent = ""; return; }
  if (!campChecked || campChecked.id !== c.id) campChecked = { id: c.id, ids: new Set(sel.items.map((x) => x.id)) };
  const src = c.ranking.source;
  $("#cp-sel-src").innerHTML = src.mode === "llm" ? `<span class="tag ai">selected by AI: ${esc(src.model)}</span>` : `<span class="tag">selected by rules</span>`;
  const covered = new Set(sel.items.flatMap((x) => x.finding_ids || [x.finding_id]));
  $("#cp-sel-note").textContent = `${sel.items.length} experiments cover ${covered.size} of ${c.scan.findings.length} weak points · one at a time · each fault held up to ${sel.items[0] ? sel.items[0].duration_s : "–"} s or until the self-healing agent fixes it`;
  const ST = { proposed: ["Proposed", "unknown"], approved: ["Queued", "warning"], declined: ["Not approved", "unknown"], running: ["Running now", "serious"], done: ["Done", "good"], failed: ["Refused / failed", "critical"], skipped: ["Skipped", "warning"] };
  const fTitle = Object.fromEntries(c.scan.findings.map((f) => [f.id, f.title]));
  let clock = Date.now() / 1000;
  const running = sel.items.find((x) => x.status === "running");
  if (running && running.started) clock = Math.max(clock, running.started + (running.est_s || 135));
  const when = (x) => {
    if (x.status === "done" || x.status === "failed") return `${hm(x.started)} → ${hm(x.ended)}` + (x.result && x.result.fault_start ? `<div class="muted">fault ${hm(x.result.fault_start)} → ${hm(x.result.fault_end)}</div>` : "");
    if (x.status === "running") return `started ${hm(x.started)}<div class="muted">running now</div>`;
    if (x.status === "approved") { const t = clock; clock += x.est_s || 135; return `≈ ${hm(t)}<div class="muted">up to ${Math.round((x.est_s || 135) / 6) / 10} min</div>`; }
    if (x.status === "proposed") return `<span class="muted">after you confirm · up to ${Math.round((x.est_s || 135) / 6) / 10} min</span>`;
    return "–";
  };
  const html = `<div class="tblwrap"><table class="tbl exps"><thead><tr>${awaiting ? "<th></th>" : ""}<th>#</th><th>What is injected</th><th>Weak points it tests</th><th>When</th><th>Status</th></tr></thead><tbody>`
    + sel.items.map((x) => {
      const tc = x.tool_call, res = x.result, ids = x.finding_ids || [x.finding_id];
      const toolNote = tc ? `<div class="small muted">injected by ${tc.source.mode === "llm" ? "the AI agent" : "rules"} via inject_fault</div>` : "";
      return `<tr class="${x.status === "running" ? "now" : ""}">
        ${awaiting ? `<td><input type="checkbox" data-exp="${esc(x.id)}" ${campChecked.ids.has(x.id) ? "checked" : ""} aria-label="approve ${esc(x.id)}"></td>` : ""}
        <td><b>${esc(x.id)}</b></td>
        <td><b>${esc(faultTitle(x))}</b><details class="small"><summary class="muted">why and what we expect</summary>${esc(humanize(x.why))}<br><span class="muted">Expected: ${esc(humanize(x.expected_effect))}</span></details></td>
        <td class="small">${ids.map((i) => `<div><b>${esc(i)}</b> ${esc(fTitle[i] || "")}</div>`).join("")}</td>
        <td class="small nowrap">${when(x)}</td>
        <td class="small">${chip(...(ST[x.status] || [x.status, "unknown"]))}${toolNote}</td></tr>`;
    }).join("") + `</tbody></table></div>` + ((c.proposals && c.proposals.rejected.length) ? `<div class="banner warning">${c.proposals.rejected.map((r) => `Dropped by the safety policy: ${esc(faultTitle(r))}: ${esc(r.errors.join("; "))}`).join("<br>")}</div>` : "");
  if (box.dataset.html !== html) { box.dataset.html = html; box.innerHTML = html; }
  const n = [...campChecked.ids].length, min = sel.min || 3;
  $("#cp-confirm").disabled = n < min;
  $("#cp-confirm").textContent = n < min ? `Select at least ${min}` : `Approve ${n} & inject`;
  $("#cp-confirm-note").textContent = awaiting ? `Uncheck any you do not want. At least ${min} are required. They run one at a time; the kill switch stops everything.` : c.approved ? `You approved ${c.approved.join(", ")}.` : "";
}
document.addEventListener("change", (e) => {
  const cb = e.target.closest("#cp-select input[data-exp]"); if (!cb || !campChecked) return;
  cb.checked ? campChecked.ids.add(cb.dataset.exp) : campChecked.ids.delete(cb.dataset.exp);
  $("#cp-select").dataset.html = ""; loadCampaign();
});

// ---- section 4: the running (or last) experiment, with the self-healing agent's full detail
function renderMoments(inc, g) {
  const m = (k, v, note) => `<div><div class="k">${k}</div><div class="v">${v}</div><div class="small muted">${note}</div></div>`;
  const att = inc && inc.attempts[inc.attempts.length - 1];
  $("#cp-moments").innerHTML = !inc ? "" :
    m("Fault injected", g.injected && g.injected.fault_start ? "0 s" : "–", g.injected ? hm(g.injected.fault_start) : "")
    + m("Detected", secs(g.time_to_detect_s), "statistics")
    + m("Diagnosed", secs(g.time_to_diagnose_s), att ? (att.source.mode === "llm" ? "AI" : "rules") : "")
    + m("Fix applied", secs(g.time_to_fix_s), att && att.runbook ? `runbook ${att.runbook.id}` : "")
    + m("Back to normal", secs(g.time_to_verified_s), inc.status === "resolved" ? "verified" : "");
}
function renderSignals(inc) {
  const att = inc && inc.attempts[inc.attempts.length - 1];
  const snap = (att && att.snapshot) || (inc && inc.detection);
  if (!snap) {
    rows($("#hl-anoms tbody"), [], 5, inc && inc.live && inc.live.anomalies ? `Latest check: ${inc.live.signals.join(" · ")} (not yet confirmed)` : "No anomaly confirmed.");
    $("#hl-normals").innerHTML = ""; $("#hl-detect-note").textContent = "An incident opens when anomalies persist on 2 consecutive checks.";
    return;
  }
  $("#hl-detect-note").textContent = `Snapshot used for ${att ? "the diagnosis" : "detection"} at ${fmtTime(snap.ts)}: last ${snap.window_s} s (${snap.samples_in_window} requests) compared with the learned baseline. Hover a cited id to highlight it.`;
  rows($("#hl-anoms tbody"), snap.anomalies.map((a) => `<tr data-sig="${esc(a.id)}"><td><span class="sigid">${esc(a.id)}</span></td><td>${esc(a.signal)}<div class="small muted">Rule: ${esc(a.rule)}</div></td><td class="nowrap">${esc(a.baseline)}</td><td class="nowrap"><b>${esc(a.observed)}</b></td><td class="nowrap">${esc(a.deviation)}</td></tr>`), 5, "none");
  $("#hl-normals").innerHTML = snap.normals.map((n) => `<li data-sig="${esc(n.id)}"><span class="sigid">${esc(n.id)}</span> ${esc(n.signal)}</li>`).join("") || `<li class="muted">nothing is still normal</li>`;
}
function renderDiagnosis(inc) {
  const atts = inc ? inc.attempts : [];
  const att = atts[atts.length - 1];
  if (!att) { $("#hl-diag").innerHTML = `<div class="empty">${inc && inc.status === "diagnosing" ? "Waiting for the diagnosis…" : "No failure diagnosed yet."}</div>`; $("#hl-diag-src").innerHTML = ""; $("#hl-runbook").innerHTML = `<div class="empty">No runbook selected yet.</div>`; return; }
  const d = att.diagnosis, src = att.source;
  $("#hl-diag-src").innerHTML = src.mode === "llm" ? `<span class="tag ai">AI: ${esc(src.model)}</span>` : `<span class="tag">${src.mode === "rules-fallback" ? "rules (AI failed)" : "rules"}</span>`;
  const conf = { high: "good", medium: "warning", low: "critical" }[d.confidence];
  const rc = att.rules_check;
  const earlier = atts.slice(0, -1).map((a) => `<div class="attempt small">Attempt ${a.n}: “${esc(CLASS[a.diagnosis.failure_class])}” by ${esc(whoDiag(a.source))} → ${esc(a.runbook.id)} → ${a.verification ? (a.verification.ok ? "fixed" : "<b>not fixed</b>; this class was excluded and the agent diagnosed again") : "…"}</div>`).join("");
  $("#hl-diag").innerHTML = `${atts.length > 1 ? `<div class="small muted">Attempt ${att.n} of ${atts.length}</div>` : ""}
    <div class="diag-class">${esc(CLASS[d.failure_class])}</div>
    <div class="row" style="align-items:center">${chip(cap(d.confidence) + " confidence", conf)} ${src.mode === "llm" ? chip(rc.agrees ? "Rules classifier agrees" : `Rules classifier says: ${CLASS[rc.failure_class]}`, rc.agrees ? "good" : "warning") : ""}</div>
    <p class="mt">${esc(humanize(d.summary))}</p>
    <h3 class="mt">How it reached this conclusion</h3>
    <ol class="reason">${d.reasoning.map((s) => `<li>${esc(humanize(s.step))} ${s.cites.map(citeBtn).join("")}</li>`).join("")}</ol>
    ${d.ruled_out.length ? `<h3>Ruled out</h3><ul class="plain">${d.ruled_out.map((r) => `<li><b>${esc(CLASS[r.failure_class])}</b>: ${esc(humanize(r.why))} ${r.cites.map(citeBtn).join("")}</li>`).join("")}</ul>` : ""}
    ${src.error ? `<div class="note">${src.mode === "llm" ? "The model's first answer was rejected and corrected: " : "The AI model could not be used, so the rules classifier decided: "}${esc(src.error)}</div>` : ""}
    ${earlier}`;
  const rb = att.runbook;
  if (!rb) { $("#hl-runbook").innerHTML = `<div class="empty">Selecting…</div>`; return; }
  const ex = att.execution;
  $("#hl-runbook").innerHTML = `<div class="small muted">Rule matched: “${esc(CLASS[rb.failure_class])}” → <b>${esc(rb.id)}</b></div>
    <div class="diag-class small-class">${esc(rb.title)}</div><p class="small">${esc(rb.why)}</p>
    ${ex ? `<ul class="steps">${ex.steps.map((s) => `<li>${chip(s.ok ? "ok" : "failed", s.ok ? "good" : "critical")} <b>${esc(s.step)}</b> <span class="muted">${esc(s.detail)}</span></li>`).join("")}</ul>` : `<div class="muted">running…</div>`}`;
}
function renderVerify(inc, g) {
  if (!inc || !inc.attempts.length) { $("#hl-verify").innerHTML = `<div class="empty">${inc && inc.status === "no_incident" ? (g.correct_no_incident ? "No user-facing impact: the agent correctly raised no incident." : "No anomaly was confirmed.") : "Nothing to verify yet."}</div>`; return; }
  const att = inc.attempts[inc.attempts.length - 1], v = att.verification;
  const checks = v ? `<ul class="steps">${v.checks.map((c) => `<li>${chip(c.ok ? "passed" : "failed", c.ok ? "good" : "critical")} <b>${esc(c.check)}</b> <span class="muted">${esc(c.detail)}</span></li>`).join("")}</ul>`
    : `<div class="muted">${inc.status === "verifying" ? "checking…" : "waiting for the fix"}</div>`;
  const by = g.remediated_by;
  const removed = !by ? "" : `<p>${chip(by === "agent" ? "Confirmed by the engine: removed by the agent" : `Removed by the ${by}`, by === "agent" ? "good" : "warning")}</p>`;
  const closed = !!inc.ended && g.injected && g.injected.target;
  const grade = !closed ? `<p class="small muted">The injected fault is hidden from the agent and graded when the incident closes.</p>`
    : `<p class="small">Actually injected: <b>${esc(faultTitle(g.injected))}</b> · agent's answer ${esc(CLASS[g.final_class] || "–")} ${g.correct == null ? "" : chip(g.correct ? "correct" : "wrong", g.correct ? "good" : "critical")}</p>`;
  $("#hl-verify").innerHTML = checks + removed + grade;
}
async function cpLive(c) {
  const items = c && c.selection ? c.selection.items : [];
  const cur = items.find((x) => x.status === "running") || [...items].reverse().find((x) => x.status === "done");
  const el = $("#cp-chart");
  $("#cp-agent").hidden = !cur;
  if (!cur || !cur.run_id) {
    el.innerHTML = `<div class="empty">The live view appears when an experiment runs.</div>`; el.dataset.sig = ""; $("#cp-wf").innerHTML = ""; $("#cp-live-status").innerHTML = ""; $("#cp-moments").innerHTML = "";
    $("#cp-live-title").textContent = "4 · How the website reacts"; renderSignals(null); renderDiagnosis(null); renderVerify(null, {}); return;
  }
  const live = cur.status === "running";
  const key = cur.run_id + "|" + (live ? Math.floor(Date.now() / 2000) : "done");
  if (campRunCache.key !== key) {
    try {
      const [run, inc] = await Promise.all([api("/api/runs/" + encodeURIComponent(cur.run_id)), api(`/api/incidents/inc-${encodeURIComponent(cur.run_id)}`).catch(() => null)]);
      campRunCache = { key, run, inc: inc && inc.incident, grade: (inc && inc.grade) || {} };
    } catch { return; }
  }
  const r = campRunCache.run, inc = campRunCache.inc, g = campRunCache.grade;
  $("#cp-live-title").textContent = `4 · How the website reacts: ${cur.id} ${faultTitle(cur)}${live ? " (live)" : " (last run)"}`;
  const hs = inc ? HSTATUS[inc.status] || [inc.status, "unknown"] : ["starting", "unknown"];
  $("#cp-live-status").innerHTML = chip(`Self-healing agent: ${hs[0]}`, hs[1]);
  const att = inc && inc.attempts[inc.attempts.length - 1];
  const marks = inc ? [[inc.detected_at, "detected"], [att && att.remediated_at, "fix applied"], [inc.resolved_at, "verified"]].filter((m) => m[0]).map(([t, label]) => ({ t, label })) : [];
  const sig = [r.id, r.samples.length, r.fault_end, marks.length].join("|");
  if (el.dataset.sig !== sig) {
    el.dataset.sig = sig;
    latencyChart(el, r.samples, { faultStart: r.fault_start, faultEnd: r.fault_end, marks, now: live ? Date.now() / 1000 : null, height: 300 });
  }
  renderWorkflows($("#cp-wf"), r.samples);
  renderMoments(inc, g); renderSignals(inc); renderDiagnosis(inc); renderVerify(inc, g);
}

// ---- section 5: findings, one row per weak point
function findingVerdict(c, f) {
  const items = ((c.selection || {}).items || []).filter((x) => (x.finding_ids || [x.finding_id]).includes(f.id) && x.status !== "declined");
  const done = items.filter((x) => x.result);
  const hit = done.filter((x) => ["full", "partial"].includes(x.result.analyst.impact));
  const worst = [...done].sort((a, b) => (SEV_RANK[b.result.analyst.severity] || 0) - (SEV_RANK[a.result.analyst.severity] || 0))[0];
  return { items, done, hit, worst, confirmed: hit.length > 0, pending: items.length && done.length < items.length };
}
function cpFindingsTable(c) {
  const tbody = $("#cp-ftable tbody");
  if (!c || !c.scan.findings.length || !c.selection) { rows(tbody, [], 6, "Findings appear here as experiments finish."); return; }
  const list = c.scan.findings.map((f) => ({ f, v: findingVerdict(c, f) }))
    .sort((a, b) => (b.v.done.length ? 1 : 0) - (a.v.done.length ? 1 : 0) || (SEV_RANK[(b.v.worst || { result: { analyst: {} } }).result.analyst.severity] || 0) - (SEV_RANK[(a.v.worst || { result: { analyst: {} } }).result.analyst.severity] || 0));
  const html = list.map(({ f, v }) => {
    const tested = v.items.map((x) => `${x.id} ${faultTitle(x).toLowerCase()}`).join(", ") || "not tested";
    if (!v.done.length) {
      const st = !v.items.length ? ["Not tested", "unknown"] : v.items.some((x) => x.status === "running") ? ["Running now", "serious"] : ["Queued", "unknown"];
      return `<tr class="sev-pending"><td><b>${esc(f.title)}</b></td><td class="small">${esc(tested)}</td><td>${chip(...st)}</td><td class="small muted">–</td><td class="small muted">–</td><td class="small muted">–</td></tr>`;
    }
    const w = v.worst.result, an = w.analyst, ag = w.agent, ph = w.phases, sev = an.severity || "none";
    const verdictChip = v.confirmed ? chip(`Confirmed · ${cap(sev)}`, SEV_TONE[sev]) : chip("Not confirmed", "good");
    const err = (p) => (p && p.n ? fmtPct(p.error_rate) : "–");
    const broke = [...(an.failed || []), ...(an.degraded || [])].map((x) => WORKFLOWS[x] || x);
    let what = `Errors ${err(ph.before)} → <b>${err(ph.during)}</b> → ${err(ph.after)}.` + (broke.length ? ` Affected: ${esc(broke.join(", "))}.` : " No workflow was affected.");
    if (f.category === "circuit_breaker" && w.circuit_breaker) what += ` ${esc(cap(w.circuit_breaker.verdict))}.`;
    const heal = ag.remediated_by === "agent" ? `${chip(ag.time_to_verified_s != null ? `fixed in ${secs(ag.time_to_verified_s)}` : "fault removed by the agent", "good")}<br><span class="muted">${ag.status === "resolved" ? (ag.correct ? "correct diagnosis" : "diagnosis was wrong") : "agent did not confirm its own fix"}</span>`
      : ag.correct_no_incident ? `${chip("nothing to fix", "good")}<br><span class="muted">correctly stayed quiet</span>`
      : ag.final_class ? `${chip("diagnosed, fixed late", "warning")}<br><span class="muted">${ag.correct ? "correct diagnosis" : "wrong diagnosis"}; removed by the ${esc(ag.remediated_by || "engine")}</span>`
      : `${chip(HSTATUS[ag.status] ? cap(HSTATUS[ag.status][0]) : "–", "warning")}`;
    const cls = !v.confirmed ? "sev-low" : SEV_RANK[sev] >= 4 ? "sev-crit" : SEV_RANK[sev] === 3 ? "sev-high" : "sev-low";
    return `<tr class="${cls}"><td><b>${esc(f.title)}</b></td><td class="small">${esc(tested)}</td><td>${verdictChip}</td><td class="small">${what}</td><td class="small">${heal}</td><td class="small">${an.fix ? esc(humanize(an.fix)) : "–"}</td></tr>`;
  });
  rows(tbody, html, 6, "");
}
async function loadCampaign() {
  let d; try { d = await api("/api/campaign/latest"); } catch (e) { $("#cp-text").textContent = "Unavailable: " + e.message; return; }
  const c = d.campaign;
  const [st, text] = cpHero(c);
  $("#cp-status").innerHTML = chip(st[0], st[1]); $("#cp-text").textContent = text;
  $("#cp-start").disabled = !cpDone(c);
  $("#cp-start").textContent = c && cpDone(c) ? "Start a new scan" : "Start resilience scan";
  $("#cp-cancel").hidden = cpDone(c);
  $("#cp-cancel").textContent = c && c.status === "running" ? "Stop after this experiment" : "Cancel";
  $("#cp-meta").textContent = c ? `${c.id} · started ${fmtTime(c.created)}` : "";
  cpKpis(c); cpFlow(c); cpScan(c); cpFindings(c); cpSelection(c); cpFindingsTable(c); cpLive(c);
}
$("#cp-start").addEventListener("click", async () => {
  try { await api("/api/campaign", { method: "POST" }); campChecked = null; toast("Resilience scan started"); }
  catch (e) { toast("Not started: " + e.message, 9000); }
  loadCampaign();
});
$("#cp-cancel").addEventListener("click", async () => {
  const c = (await api("/api/campaign/latest").catch(() => ({}))).campaign; if (!c) return;
  try { await api(`/api/campaign/${encodeURIComponent(c.id)}/cancel`, { method: "POST" }); toast(c.status === "running" ? "The campaign stops after the running experiment finishes safely." : "Campaign cancelled."); }
  catch (e) { toast("Cancel failed: " + e.message); }
  loadCampaign();
});
$("#cp-confirm").addEventListener("click", async () => {
  const c = (await api("/api/campaign/latest").catch(() => ({}))).campaign; if (!c || !campChecked) return;
  const approved = c.selection.items.map((x) => x.id).filter((id) => campChecked.ids.has(id));
  try { await api(`/api/campaign/${encodeURIComponent(c.id)}/confirm`, { method: "POST", body: { approved } }); toast(`Approved ${approved.join(", ")}: injecting one at a time`); }
  catch (e) { toast("Not started: " + e.message, 10000); }
  loadCampaign();
});

// ---------------------------------------------------------------- refresh loop
async function tickOverview() {
  if (currentTab !== "overview") return;
  let v; try { v = await getView(); } catch { return; }
  lastView = v;
  renderHealth(v); renderOverviewRun(v); renderLog(v);
}
async function refreshAll() {
  await loadOverview().catch(() => {});
  if (currentTab === "overview") { await tickOverview(); loadGraph(); loadMetrics(); loadScore(); }
  if (currentTab === "campaign") loadCampaign();
}
(async function init() {
  legend($("#lg-ov")); legend($("#lg-cp"), true);
  loadAi();
  const wanted = location.hash.slice(1);
  if (wanted === "campaign") showTab("campaign"); else await refreshAll();
  setInterval(tickOverview, 2000);
  setInterval(() => { if (currentTab === "campaign") loadCampaign(); }, 1500);
  setInterval(() => loadOverview().catch(() => {}), 3000);
  setInterval(() => { if (currentTab === "overview") { loadGraph(); loadMetrics(); } }, 5000);
  setInterval(() => { if (currentTab === "overview") loadScore(); }, 15000);
  setInterval(loadAi, 30000);
})();
