"use strict";
// Paperless Chaos Lab dashboard. Every value shown comes from the controller
// API; when data is missing or the backend is unreachable we say so.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "–");
const fmtMs = (v) => (v == null ? "–" : v >= 1000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms");
const fmtPct = (v) => (v == null ? "–" : (v * 100).toFixed(v > 0 && v < 0.1 ? 1 : 0) + "%");
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const PROBES = ["web_root", "api_documents", "api_status", "doc_upload"];
// Plain names for what the probes and services are.
const WORKFLOWS = { web_root: "Open the web UI", api_documents: "List documents", api_status: "System status check", doc_upload: "Upload a document", doc_processing: "Process a document" };
const SERVICE = { db: "database", broker: "message broker", webserver: "web application", host: "users" };
const PROBE_VAR = { web_root: "--s1", api_documents: "--s2", api_status: "--s3", doc_upload: "--s4" };
const TERMINAL = ["completed", "aborted", "failed", "rejected", "interrupted"];

let catalog = null, currentTab = "overview", safetyState = null, findingsData = null, planData = null, monitor = null;
let planChecked = null, selectedRun = null, lastView = null, sequenceRunning = false;

const svc = (id) => SERVICE[id] || id;
// Generated text uses internal ids; show the plain names instead.
function humanize(text) {
  return String(text ?? "")
    .replace(/\b(web_root|api_documents|api_status|doc_upload|doc_processing)\b/g, (m) => `"${WORKFLOWS[m]}"`)
    .replace(/\bdb\b/g, "database").replace(/\bbroker\b/g, "message broker")
    .replace(/\b(the|a|an) the /gi, "the ").replace(/ ?\(criticality reach [\d.]+\)/g, "").replace(/ ?Priority score [\d.]+\./g, "")
    .replace(/ exp-\d{8}-\d{6}-[0-9a-f]{4}/g, "");
}
const isLatency = (c) => c.fault_type === "network_latency";
// "Database pause" / "Message broker +300 ms latency"
const faultTitle = (c) => (c && c.target ? `${cap(svc(c.target))} ${isLatency(c) ? `${c.latency_ms ? "+" + c.latency_ms + " ms " : ""}latency` : "pause"}` : "Experiment");
// "Pausing the database" / "Slowing the database by 300 ms"
const faultDoing = (c) => (isLatency(c) ? `Slowing the ${svc(c.target)} by ${c.latency_ms} ms` : `Pausing the ${svc(c.target)}`);
const faultIs = (c) => (isLatency(c) ? `The ${svc(c.target)} is answering ${c.latency_ms} ms slower` : `The ${svc(c.target)} is frozen`);

// ---------------------------------------------------------------- utilities
async function api(path, opts = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...opts };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  let res;
  try { res = await fetch(path, init); } catch (e) { setConn(false); throw new Error("controller unreachable"); }
  setConn(true);
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
function setConn(ok) { $("#conn").innerHTML = chip(ok ? "connected" : "controller unreachable", ok ? "good" : "critical"); }
let toastTimer;
function toast(msg, ms = 5000) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), ms);
}
// Status chip: a coloured dot plus a label in normal ink (never colour alone).
const chip = (label, tone) => `<span class="status st-${tone || "unknown"}">${esc(label)}</span>`;
function toneOf(s) {
  s = String(s || "").toLowerCase();
  if (["healthy", "running", "ok", "completed", "executed", "external", "recovered"].includes(s)) return "good";
  if (["starting", "baseline", "preflight", "observing", "verifying_recovery", "rolling_back", "aborted", "skipped"].includes(s)) return "warning";
  if (["unhealthy", "injecting", "active"].includes(s)) return "serious";
  if (["paused", "exited", "dead", "missing", "failed", "rejected", "interrupted", "stopped"].includes(s)) return "critical";
  return "unknown";
}
const SEV_TONE = { critical: "critical", high: "serious", medium: "warning", low: "good", none: "good", inconclusive: "unknown" };
const sevChip = (s) => chip(s.score == null ? "Inconclusive" : `${cap(s.category)} (${Math.round(s.score)}/100)`, SEV_TONE[s.category]);
const SCOPE = { full: ["Full outage", "critical"], partial: ["Partial", "warning"], none: ["No impact", "good"], inconclusive: ["Inconclusive", "unknown"] };
function rows(tbody, html, cols, emptyMsg) {
  tbody.innerHTML = html.length ? html.join("") : `<tr><td colspan="${cols}" class="muted">${esc(emptyMsg)}</td></tr>`;
}
function pctl(values, p) {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  return s[Math.max(0, Math.min(s.length - 1, Math.ceil((p / 100) * s.length) - 1))];
}
const list = (a) => (a.length <= 1 ? a.join("") : a.slice(0, -1).join(", ") + " and " + a[a.length - 1]);

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

// ---------------------------------------------------------------- the experiment on screen
// One shape for it: the live run if this controller has one, otherwise the
// most recent recorded experiment.
let lastRunCache = { key: null, run: null };
async function getView() {
  const d = await api("/api/experiments/current");
  const live = d.live || {};
  if (live.run_id && live.kind === "experiment") {
    const ev = live.events || [];
    const find = (k) => ev.find((e) => e.kind === k);
    const rec = find("recovery_verified"), recFail = find("recovery_failed");
    const m = rec && /recovered ([\d.]+)s/.exec(rec.message);
    return {
      busy: d.busy, source: "live", run_id: live.run_id, state: live.state, config: live.config || {}, message: live.message,
      fault_start: live.fault_start, fault_end: live.fault_end, planned_end: live.planned_end, samples: live.samples || [], events: ev,
      breaches: live.breaches || [], result: live.result,
      removal: find("fault_removed") ? true : find("rollback_failed") ? false : null,
      recovery: { verified: rec ? true : recFail ? false : null, time: m ? +m[1] : null, note: (rec || recFail || {}).message },
    };
  }
  if (d.busy) return { busy: true, source: "other", state: live.state || "running", config: live.config || {}, samples: [], events: [], message: live.message || "A baseline or another controller is running.", recovery: {}, breaches: [] };
  const runs = await api("/api/runs?kind=experiment&limit=10");
  const head = runs.find((r) => r.state !== "rejected");
  if (!head) return null;
  const key = head.id + head.state + head.ended;
  if (lastRunCache.key !== key) lastRunCache = { key, run: await api("/api/runs/" + encodeURIComponent(head.id)) };
  const r = lastRunCache.run, o = r.outcome || {}, rec = o.recovery || {};
  return {
    busy: false, source: "last", run_id: r.id, state: r.state, config: r.config || {}, fault_start: r.fault_start, fault_end: r.fault_end,
    samples: r.samples || [], events: r.events || [], breaches: o.breaches || [], result: o.result, message: o.abort_reason || "",
    removal: (o.fault_removal || {}).verified ?? null,
    recovery: { verified: rec.verified ?? null, time: rec.recovery_time_s ?? null, note: rec.notes },
  };
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
// Per-workflow effect, computed from this run's samples.
function workflowRows(v) {
  return Object.entries(WORKFLOWS).map(([p, label]) => {
    const base = phaseStats(v.samples, [p], ["baseline"]), fault = phaseStats(v.samples, [p], ["fault"]), after = phaseStats(v.samples, [p], ["recovery", "post"]);
    return { p, label, base, fault, after, vb: verdict(base), vf: verdict(fault, base), va: verdict(after, base) };
  });
}
// How many workflows broke / slowed. Uses the backend's analysis when the run has one.
function impactCounts(v) {
  const f = findingsData && findingsData.findings.find((x) => x.run_id === v.run_id);
  if (f && f.deterministic.impact !== "inconclusive") return { failed: f.deterministic.failed.map((w) => WORKFLOWS[w]), slowed: f.deterministic.degraded.map((w) => WORKFLOWS[w]), total: f.deterministic.workflows.filter((w) => w.status !== "no_data").length, analysed: true };
  const r = workflowRows(v).filter((w) => w.vf);
  return { failed: r.filter((w) => w.vf[0] === "Failing").map((w) => w.label), slowed: r.filter((w) => ["Slower", "Some errors"].includes(w.vf[0])).map((w) => w.label), total: r.length, analysed: false };
}
// Headline status. "Recovered" is only ever shown when the backend has verified recovery.
function statusOf(v) {
  if (safetyState && safetyState.blocked) return { label: "Blocked", tone: "critical" };
  if (!v) return { label: "Ready", tone: "good" };
  const s = v.state;
  if (v.source === "other" || s === "preflight" || s === "baseline") return { label: "Running", tone: "warning" };
  if (s === "injecting" || s === "active") return { label: "Fault active", tone: "serious" };
  if (s === "rolling_back" || s === "verifying_recovery") return { label: "Recovering", tone: "warning" };
  if (s === "observing") return { label: "Recovered", tone: "good" };
  if (s === "failed") return { label: "Failed", tone: "critical" };
  if (s === "interrupted") return { label: "Interrupted", tone: "critical" };
  if (!v.fault_start) return { label: "Ready", tone: "good" };
  if (v.recovery.verified === true) return { label: "Recovered", tone: "good" };
  return { label: "Finished", tone: "unknown" };
}
function heroText(v, st) {
  if (st.label === "Blocked") return `${safetyState.blocked.reason}. No experiment can start until the environment has been verified.`;
  if (!v) return "No experiment has run yet. Generate a plan and run it to see how the application copes with failure.";
  if (v.source === "other") return v.message;
  const c = v.config, s = v.state;
  if (s === "preflight" || s === "baseline") return `Measuring normal behaviour before the fault. Next: ${faultDoing(c).toLowerCase()} for ${c.duration_s} seconds.`;
  if (s === "injecting" || s === "active") return `${faultIs(c)}. Watching how the application reacts.`;
  if (s === "rolling_back") return `Removing the fault from the ${svc(c.target)}.`;
  if (s === "verifying_recovery") return "The fault has been removed. Checking that the application works again.";
  if (s === "failed") return `${faultDoing(c)}: the fault removal or the recovery could NOT be verified.`;
  if (s === "interrupted") return `${faultDoing(c)}: the controller stopped in the middle of this run.`;
  if (!v.fault_start) return `The last run stopped before any fault was injected (${v.message || v.result || s}).`;
  const k = impactCounts(v);
  const effect = !k.total ? "had no measurable effect"
    : k.failed.length ? `broke ${k.failed.length} of ${k.total} user workflows` + (k.slowed.length ? ` and slowed ${k.slowed.length} more` : "")
    : k.slowed.length ? `slowed ${k.slowed.length} of ${k.total} user workflows` : "had no effect on any user workflow";
  const rec = v.recovery.verified === true ? ` The application recovered${v.recovery.time != null ? ` ${v.recovery.time.toFixed(1)} seconds` : ""} after the fault was removed.`
    : s === "observing" ? "" : " Recovery has not been confirmed.";
  return `${faultDoing(c)} ${effect}.${rec}`;
}
// The five-step story of one experiment.
function story(v) {
  const todo = (title, hint) => ({ title, value: "–", note: hint, cls: "todo" });
  if (!v || v.source === "other") return [todo("Baseline", "measure normal behaviour"), todo("Inject fault", "pause or slow a dependency"), todo("Observe impact", "errors and slow-downs"), todo("Roll back", "remove the fault"), todo("Recover", "verify the app works again")];
  const c = v.config, s = v.state, now = Date.now() / 1000, done = TERMINAL.includes(s);
  const base = phaseStats(v.samples, PROBES, ["baseline"]), fault = phaseStats(v.samples, PROBES, ["fault"]);
  const injected = !!v.fault_start, removed = !!v.fault_end;
  const steps = [];
  steps.push({ title: "Baseline", value: base ? `${fmtPct(base.err)} errors` : "measuring…", note: base ? `normal response time ${fmtMs(base.p95)}` : "normal behaviour before the fault",
    cls: injected || done ? "done" : "now" });
  const secs = injected ? (v.fault_end || now) - v.fault_start : null;
  steps.push(!injected ? todo("Inject fault", `${faultTitle(c)}, ${c.duration_s} s`) : { title: "Fault injected", value: isLatency(c) ? `+${c.latency_ms} ms on ${c.target}` : `${cap(c.target)} frozen`,
    note: removed ? `active for ${secs.toFixed(0)} s of ${c.duration_s} s planned` : `active for ${secs.toFixed(0)} s of at most ${c.duration_s} s`, cls: removed ? "done" : "now" });
  if (!injected) steps.push(todo("Observe impact", "errors and slow-downs"));
  else {
    const k = impactCounts(v);
    const value = !fault ? "watching…" : k.failed.length || k.slowed.length ? [k.failed.length ? `${k.failed.length} failing` : "", k.slowed.length ? `${k.slowed.length} degraded` : ""].filter(Boolean).join(", ") : removed ? "no impact" : "none so far";
    steps.push({ title: "Impact observed", value, note: v.breaches.length ? "stopped early by the safety threshold" : fault ? `failed requests ${fmtPct(base && base.err)} → ${fmtPct(fault.err)}` : "waiting for requests to finish",
      cls: !removed ? "now" : k.failed.length ? "done bad" : "done" });
  }
  steps.push(!removed ? todo("Roll back", "remove the fault") : v.removal === true ? { title: "Rolled back", value: "Fault removed", note: `verified at ${fmtTime(v.fault_end)}`, cls: "done" }
    : v.removal === false ? { title: "Roll back", value: "NOT verified", note: "the fault may still be present", cls: "done bad" } : { title: "Rolling back", value: "removing…", note: "", cls: "now" });
  steps.push(!removed ? todo("Recover", "verify the app works again") : v.recovery.verified === true ? { title: "Recovered", value: v.recovery.time != null ? `in ${v.recovery.time.toFixed(1)} s` : "verified", note: "application healthy again (verified)", cls: "done" }
    : v.recovery.verified === false ? { title: "Recovery", value: "NOT verified", note: v.recovery.note || "", cls: "done bad" } : { title: "Recovering", value: "checking…", note: "waiting for healthy responses", cls: "now" });
  return steps;
}
function renderStory(el, steps) {
  el.innerHTML = steps.map((s, i) => `<li class="${s.cls}"><span class="n">${s.cls.includes("done") ? (s.cls.includes("bad") ? "!" : "✓") : i + 1}</span><div><div class="t">${esc(s.title)}</div><div class="v">${esc(s.value)}</div><div class="h">${esc(s.note)}</div></div></li>`).join("");
}
function renderWorkflows(el, v) {
  if (!v || v.source === "other" || !v.samples.length) { el.innerHTML = `<div class="empty">No measurements yet.</div>`; return; }
  const c = (vd, st) => (vd ? `<td title="${fmtPct(st.err)} of requests failed · response time ${fmtMs(st.p95)}">${chip(vd[0], vd[1])} <span class="small muted">${st.err > 0 ? fmtPct(st.err) + " errors" : fmtMs(st.p95)}</span></td>` : `<td class="muted">–</td>`);
  el.innerHTML = `<table class="tbl wf"><thead><tr><th>Workflow</th><th>Before</th><th>During the fault</th><th>After</th></tr></thead><tbody>`
    + workflowRows(v).map((w) => `<tr><td><b>${esc(w.label)}</b></td>${c(w.vb, w.base)}${c(w.vf, w.fault)}${c(w.va, w.after)}</tr>`).join("") + `</tbody></table>`;
}

// ---------------------------------------------------------------- overview
const LOG = {
  preflight_passed: ["Preflight passed", "good"], fault_injected: ["Fault injected", "serious"],
  threshold_breach: ["Automatic abort", "critical"], manual_abort: ["Aborted by kill switch", "warning"], kill_switch: ["Kill switch", "warning"],
  fault_removed: ["Rollback verified", "good"], rollback_failed: ["Rollback FAILED", "critical"], recovery_verified: ["Recovery verified", "good"],
  recovery_failed: ["Recovery NOT verified", "critical"], blocked: ["Experiments blocked", "critical"], unblocked: ["Unblocked", "warning"],
  finished: ["Run finished", "unknown"], analysis_done: ["Analysed", "unknown"], aborted: ["Aborted", "warning"], rejected: ["Rejected by safety", "warning"],
  kill_rollback: ["Kill-switch rollback", "warning"], controller_error: ["Controller error", "critical"], fault_error: ["Fault error", "critical"],
};
function renderLog(v) {
  const events = v && v.events.length ? v.events : (safetyState ? safetyState.events : []);
  const out = events.filter((e) => LOG[e.kind]).sort((a, b) => b.ts - a.ts);
  rows($("#ov-log tbody"), out.slice(0, 20).map((e) => `<tr><td class="muted nowrap">${fmtTime(e.ts)}</td><td class="nowrap">${chip(...LOG[e.kind])}</td><td class="small">${esc(e.message)}</td></tr>`), 3, "no events yet");
}
function renderHero(v) {
  const st = statusOf(v);
  $("#hero-status").innerHTML = chip(st.label, st.tone);
  $("#hero-text").textContent = heroText(v, st);
  $("#story-title").textContent = !v || v.source === "other" ? "How an experiment works" : `${v.busy ? "Running now" : "Latest experiment"}: ${faultTitle(v.config)}`;
  const busy = v && v.busy;
  $("#hero-cta").textContent = busy ? "View experiments →" : "Run an experiment →";
  const m = monitor || {}, svcs = m.services || [], bad = svcs.filter((s) => !s.ok);
  const app = !m.ts ? chip("Application: waiting for first check", "unknown") : chip(m.app_ok ? "Application healthy" : "Application degraded", m.app_ok ? "good" : "critical");
  const box = m.docker_error ? chip("Docker unreachable", "critical") : !svcs.length ? chip(m.ts ? "Sandbox not running" : "Sandbox: waiting", m.ts ? "critical" : "unknown")
    : chip(bad.length ? `${bad.length} of ${svcs.length} containers unhealthy` : `All ${svcs.length} containers healthy`, bad.length ? "critical" : "good");
  const safe = safetyState && safetyState.blocked ? chip("Safety: blocked", "critical") : safetyState && safetyState.active_fault ? chip(`Fault active on ${safetyState.active_fault.service}`, "serious") : chip("Safety controls armed", "good");
  $("#hero-health").innerHTML = app + box + safe;
}
async function loadOverview() {
  let d;
  try { d = await api("/api/overview"); } catch (e) { $("#hero-health").innerHTML = chip("Controller unreachable: " + e.message, "critical"); return; }
  monitor = d.monitor; safetyState = d.safety;
  $("#blocked-banner").hidden = !d.safety.blocked;
  if (d.safety.blocked) $("#blocked-reason").textContent = `${d.safety.blocked.reason} (${fmtTime(d.safety.blocked.ts)}). No experiment can start until the environment is verified.`;
}
function renderScore() {
  if (!findingsData) return;
  const o = findingsData.overall;
  $("#hero-score").innerHTML = o.score == null ? `<span class="muted">n/a</span>` : `${Math.round(o.score)}<span class="of"> / 100</span>`;
  $("#hero-cov").textContent = o.score == null ? "no scored experiment yet" : `${o.scenarios_scored} of ${o.scenarios_total} failure scenarios tested` + (o.capped_unrecovered ? " · capped: a run did not recover" : "");
}
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
  for (let i = 0; i <= 4; i++) {
    const v = (ymax / 4) * i, y = sy(v);
    g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y + 4}" text-anchor="end" font-size="12" fill="var(--muted)">${fmtMs(v)}</text>`;
  }
  for (const t of timeTicks(t0, t1)) g += `<text x="${sx(t)}" y="${H - 8}" text-anchor="middle" font-size="12" fill="var(--muted)">${new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</text>`;
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
function legend(el) {
  el.innerHTML = PROBES.map((p) => `<span><span class="sw" style="background:var(${PROBE_VAR[p]})"></span>${WORKFLOWS[p]}</span>`).join("") + `<span><b style="color:var(--critical)">✕</b> failed request</span><span><span class="sw" style="background:var(--fault-band);height:10px"></span>fault active</span>`;
}
function renderChart(v) {
  const el = $("#chart-ov");
  if (!v || v.source === "other" || !v.samples.length) { el.dataset.sig = ""; el.innerHTML = `<div class="empty">The chart appears when an experiment runs.</div>`; return; }
  const sig = [v.run_id, v.samples.length, v.fault_end, v.recovery.time].join("|");
  if (el.dataset.sig === sig) return;   // unchanged: keep the hover state
  el.dataset.sig = sig;
  latencyChart(el, v.samples, { faultStart: v.fault_start, faultEnd: v.fault_end, recoveredAt: v.recovery.time != null && v.fault_end ? v.fault_end + v.recovery.time : null, now: v.busy ? Date.now() / 1000 : null, height: 330 });
}

// ---------------------------------------------------------------- experiments: plan
const sourceLabel = (src) => (src.mode === "llm" ? `AI model ${src.provider}:${src.model}` : "deterministic rules (no LLM)");
function rulesNotice(src, what) {
  if (src.mode === "llm") return "";
  return src.error ? `${what} produced by deterministic rules because the LLM provider failed: ${src.error}`
    : `${what} produced by deterministic rules, not by an AI model: no LLM provider is configured.`;
}
async function loadPlan() {
  let d;
  try { d = await api("/api/plan"); } catch (e) { $("#pl-source").textContent = "unavailable: " + e.message; return; }
  planData = d.plan; sequenceRunning = d.sequence_running;
  const p = d.plan, ex = p && p.execution, blocked = safetyState && safetyState.blocked;
  $("#pl-empty").hidden = !!p; $("#pl-recs").hidden = !p;
  $("#pl-generate").disabled = sequenceRunning;
  if (!p) { $("#pl-source").textContent = ""; $("#pl-notice").hidden = true; $("#pl-rejected").hidden = true; $("#pl-run").disabled = true; $("#pl-seq").textContent = ""; return; }
  if (!planChecked || planChecked.id !== p.id) planChecked = { id: p.id, ranks: new Set(p.recommendations.filter((r) => r.status === "planned").map((r) => r.rank)) };
  $("#pl-source").textContent = `Ranked by ${sourceLabel(p.source)} at ${fmtTime(p.created)}, from the dependency map and ${p.context.previous_experiments} earlier experiments.`;
  const notice = rulesNotice(p.source, "This plan was");
  $("#pl-notice").hidden = !notice; $("#pl-notice").textContent = notice;
  const byRun = Object.fromEntries(((findingsData || {}).findings || []).map((f) => [f.run_id, f]));
  const approved = new Set(ex && ex.status === "running" ? ex.selected : []);
  const html = p.recommendations.map((r) => {
    const f = r.run_id && byRun[r.run_id];
    const status = r.status === "planned" ? (approved.has(r.rank) ? ["Approved, waiting", "warning"] : ["Recommended", "unknown"])
      : r.status === "running" ? ["Running now", "serious"] : r.status === "executed" ? ["Completed", "good"]
      : r.status === "rejected" ? ["Rejected by safety checks", "critical"] : ["Skipped", "warning"];
    const result = f ? `${chip(SCOPE[f.deterministic.impact][0], SCOPE[f.deterministic.impact][1])}${sevChip(f.severity)}<span class="small muted">${f.deterministic.recovery_verified ? "recovered" + (f.deterministic.recovery_time_s != null ? " in " + f.deterministic.recovery_time_s.toFixed(1) + " s" : "") : "recovery NOT verified"}</span>`
      : r.status === "executed" ? `<span class="muted small">not analysed yet</span>` : "";
    const selectable = r.status === "planned" && !sequenceRunning;
    const affects = r.workflows.map((w) => WORKFLOWS[w] || w);
    return `<div class="plan-row ${r.status === "running" ? "now" : ""}">
      <input type="checkbox" data-rank="${r.rank}" ${planChecked.ranks.has(r.rank) && selectable ? "checked" : ""} ${selectable ? "" : "disabled"} aria-label="select experiment ${r.rank}">
      <div class="rank">${r.rank}</div>
      <div class="what"><div class="t">${esc(faultTitle(r))} <span class="muted">for ${r.duration_s} s</span></div>
        ${affects.length ? `<div class="small">Expected to affect: ${esc(affects.join(", "))}</div>` : ""}
        <div class="small muted why">Why: ${esc(humanize(r.rationale))}</div>
        <details class="small"><summary class="muted">hypothesis and safety limits</summary>${esc(humanize(r.hypothesis))}<br>Stops automatically at ${fmtPct(r.abort_max_error_rate)} errors or ${fmtMs(r.abort_max_p95_latency_ms)} response time.${r.risks.length ? "<br>Risks: " + esc(r.risks.join("; ")) : ""}</details></div>
      <div class="state">${chip(...status)}<div class="result">${result}</div></div></div>`;
  }).join("");
  const box = $("#pl-recs");
  if (box.dataset.html !== html) { box.dataset.html = html; box.innerHTML = html || `<div class="empty">The plan has no valid recommendations.</div>`; }
  const rej = p.rejected || [];
  $("#pl-rejected").hidden = !rej.length;
  $("#pl-rejected").innerHTML = rej.map((r) => `<div>Dropped by safety validation: <b>${esc(r.name)}</b>: ${esc(r.errors.join("; "))}</div>`).join("");
  const n = [...planChecked.ranks].filter((k) => p.recommendations.find((r) => r.rank === k && r.status === "planned")).length;
  $("#pl-run").disabled = sequenceRunning || !!blocked || !n;
  $("#pl-run").textContent = sequenceRunning ? "Experiments running…" : n ? `Approve & run ${n} experiment${n > 1 ? "s" : ""}` : "Nothing selected";
  $("#pl-seq").textContent = blocked ? "Experiments are blocked: verify the environment first."
    : sequenceRunning ? `Running experiment ${ex.current ?? "…"} (approved: ${ex.selected.join(", ")}). One at a time, each checked by the safety controller first.`
    : ex ? `Last sequence ${ex.status}${ex.stop_reason ? ": " + ex.stop_reason : ""}.`
    : "Nothing runs until you approve it. Experiments run one at a time, each checked by the safety controller first.";
}
document.addEventListener("change", (e) => {
  const c = e.target.closest("#pl-recs input[data-rank]"); if (!c || !planChecked) return;
  c.checked ? planChecked.ranks.add(+c.dataset.rank) : planChecked.ranks.delete(+c.dataset.rank);
  loadPlan();
});
$("#pl-generate").addEventListener("click", async () => {
  const b = $("#pl-generate"); b.disabled = true; b.textContent = "Planning…";
  try { const p = await api("/api/plan", { method: "POST" }); planChecked = null; toast(`Plan ready: ${p.recommendations.length} experiments, ranked by ${sourceLabel(p.source)}`); }
  catch (e) { toast("Planning failed: " + e.message); }
  finally { b.textContent = "Generate new plan"; loadPlan(); }
});
$("#pl-run").addEventListener("click", async () => {
  if (!planData || !planChecked) return;
  const ranks = [...planChecked.ranks].sort((a, b) => a - b);
  try { await api(`/api/plan/${encodeURIComponent(planData.id)}/run`, { method: "POST", body: { ranks } }); toast("Approved and started: experiments " + ranks.join(", ")); }
  catch (e) { toast("Not started: " + e.message, 10000); }
  refreshAll();
});

// ---------------------------------------------------------------- experiments: run history
async function loadRuns() {
  let runs;
  try { runs = await api("/api/runs?limit=40"); } catch (e) { $("#obs-empty").hidden = false; $("#obs-empty").textContent = "Cannot load runs: " + e.message; return; }
  const sel = $("#obs-run");
  $("#obs-empty").hidden = runs.length > 0;
  const sig = runs.map((r) => r.id + r.state).join();
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  sel.innerHTML = runs.map((r) => `<option value="${esc(r.id)}">${fmtTime(r.created)} · ${r.kind === "experiment" ? esc(faultTitle(r.config)) : "baseline"} · ${esc(r.result || r.state)}</option>`).join("");
  if (!runs.length) return;
  if (!selectedRun || !runs.find((r) => r.id === selectedRun)) selectedRun = (runs.find((r) => r.kind === "experiment" && r.state !== "rejected") || runs[0]).id;
  sel.value = selectedRun;
  loadRun(selectedRun);
}
$("#obs-run").addEventListener("change", (e) => { selectedRun = e.target.value; loadRun(selectedRun); });
async function loadRun(id) {
  let r;
  try { r = await api("/api/runs/" + encodeURIComponent(id)); } catch (e) { toast("Cannot load run: " + e.message); return; }
  const o = r.outcome || {}, metrics = (r.summary || {}).metrics, rec = o.recovery || {}, rem = o.fault_removal || {}, isExp = r.kind === "experiment";
  $("#obs-report").hidden = $("#obs-export").hidden = !isExp;
  $("#obs-report").href = `/api/runs/${encodeURIComponent(id)}/report`; $("#obs-export").href = `/api/runs/${encodeURIComponent(id)}/export`;
  latencyChart($("#obs-latency"), r.samples, { faultStart: r.fault_start, faultEnd: r.fault_end, recoveredAt: rec.recovery_time_s != null && r.fault_end ? r.fault_end + rec.recovery_time_s : null });
  const prow = [];
  for (const [phase, ph] of Object.entries(metrics || {})) for (const [p, s] of [["all user-facing", ph.critical], ...Object.entries(ph.probes)])
    prow.push(`<tr><td>${esc(phase)}</td><td>${esc(p)}</td><td class="num">${s.n}</td><td class="num">${fmtPct(s.error_rate)}</td><td class="num">${fmtMs(s.p50_ms)}</td><td class="num">${fmtMs(s.p95_ms)}</td></tr>`);
  rows($("#obs-phases tbody"), prow, 6, metrics ? "no samples" : "summary not available (run in progress or interrupted)");
  const yn = (v) => (v == null ? chip("n/a", "unknown") : chip(v ? "yes" : "NO", v ? "good" : "critical"));
  $("#obs-recovery").innerHTML = !isExp ? `<div class="muted">Baseline run: no fault injected.</div>` : `<table class="tbl"><tbody>
    <tr><td>Result</td><td>${chip(o.result || r.state, toneOf(r.state))}</td><td class="small">${esc(o.abort_reason || "")}</td></tr>
    <tr><td>Fault injected</td><td>${yn(o.injected ?? (r.fault_start ? true : null))}</td><td class="small">${fmtTime(r.fault_start)} → ${fmtTime(r.fault_end)}</td></tr>
    <tr><td>Rollback verified</td><td>${yn(rem.verified)}</td><td class="small">${esc((rem.notes || []).join(" | "))}</td></tr>
    <tr><td>Recovery verified</td><td>${yn(rec.verified)}</td><td class="small">${rec.recovery_time_s != null ? rec.recovery_time_s.toFixed(1) + " s after rollback" : esc(rec.notes || "")}</td></tr></tbody></table>`;
  const logs = o.logs || {};
  $("#obs-logs").innerHTML = Object.keys(logs).length ? Object.entries(logs).map(([s, l]) => `<div class="small"><b>${esc(s)}</b> <span class="muted">${l.relevant.length} relevant of ${l.total} lines</span></div><pre class="logs">${esc((l.relevant.length ? l.relevant : l.tail).join("\n")) || "(no log lines)"}</pre>`).join("") : `<div class="muted small">No logs captured for this run.</div>`;
}

// ---------------------------------------------------------------- experiments: manual
async function loadCatalog() {
  try { catalog = await api("/api/catalog"); } catch (e) { toast("Cannot load catalog: " + e.message); return; }
  const f = $("#exp-form"), L = catalog.limits;
  f.fault_type.innerHTML = Object.entries(catalog.faults).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#limits-desc").textContent = `Safety limits: duration ${L.min_duration_s}–${L.max_duration_s} s; latency ${L.min_latency_ms}–${L.max_latency_ms} ms; recovery ≤ ${L.max_recovery_timeout_s} s; a watchdog removes the fault ${L.watchdog_grace_s} s after the planned end. Approved targets: ${Object.keys(catalog.targets).join(", ")}.`;
  syncTargets();
}
function syncTargets() {
  const f = $("#exp-form"), ft = f.fault_type.value, cur = f.target.value;
  f.target.innerHTML = Object.entries(catalog.targets).filter(([, fs]) => fs.includes(ft)).map(([k]) => `<option>${k}</option>`).join("");
  if (cur) f.target.value = cur;
  $$(".lat-only").forEach((x) => (x.hidden = ft !== "network_latency"));
}
$("#exp-form").fault_type.addEventListener("change", syncTargets);
function readConfig() {
  const f = $("#exp-form");
  const cfg = {
    name: f.name.value, hypothesis: f.hypothesis.value, fault_type: f.fault_type.value, target: f.target.value,
    duration_s: +f.duration_s.value, recovery_timeout_s: +f.recovery_timeout_s.value,
    thresholds: { max_error_rate: +f.max_error_rate.value / 100, max_p95_latency_ms: +f.max_p95_latency_ms.value, window_s: +f.window_s.value, min_samples: +f.min_samples.value },
  };
  if (cfg.fault_type === "network_latency") cfg.latency_ms = +f.latency_ms.value;
  return cfg;
}
function renderChecks(checks, errors) {
  $("#preflight").innerHTML = (checks || []).map((c) => `<li>${chip(c.ok ? "pass" : "FAIL", c.ok ? "good" : "critical")} ${esc(c.name)} <span class="muted">${esc(c.detail)}</span></li>`).join("");
  const box = $("#exp-errors");
  box.hidden = !(errors && errors.length);
  box.innerHTML = (errors || []).map((e) => `<div>${esc(e)}</div>`).join("");
}
$("#btn-preflight").addEventListener("click", async () => {
  $("#preflight").innerHTML = `<li class="muted">running…</li>`;
  try { const r = await api("/api/experiments/preflight", { method: "POST", body: readConfig() }); renderChecks(r.checks, r.errors); }
  catch (e) { renderChecks(e.data?.checks || [], [e.message]); }
});
$("#exp-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { const r = await api("/api/experiments", { method: "POST", body: readConfig() }); toast("Experiment started: " + r.run_id); renderChecks([], []); }
  catch (err) { renderChecks(err.data?.checks || [], err.data?.errors || [err.message]); }
  refreshAll();
});
$("#run-baseline").addEventListener("click", async () => {
  try { const r = await api("/api/baseline", { method: "POST", body: { duration_s: +$("#base-dur").value } }); toast("Baseline started: " + r.run_id); }
  catch (e) { toast("Baseline rejected: " + e.message); }
  refreshAll();
});

// ---------------------------------------------------------------- findings
const names = (a) => a.map((w) => WORKFLOWS[w] || w);
function recoveryText(det) {
  return det.recovery_verified ? `Recovered${det.recovery_time_s != null ? " in " + det.recovery_time_s.toFixed(1) + " s" : ""}` : det.injected ? "NOT verified" : "–";
}
function findingHtml(f, rank) {
  const d = f.diagnosis, det = f.deterministic, facts = Object.fromEntries(det.facts.map((x) => [x.id, x.text]));
  const ai = f.source.mode === "llm", tag = ai ? `<span class="tag ai">AI hypothesis</span>` : `<span class="tag">Rule-based hypothesis</span>`;
  const rc = d.root_causes[0];
  const li = (arr, tone, word) => arr.map((w) => `<li>${chip(word, tone)} ${esc(w)}</li>`).join("");
  return `<div class="card fcard">
    <div class="fhead">${rank ? `<div class="rank">${rank}</div>` : ""}<h2>${esc(faultTitle(det))}</h2>${sevChip(f.severity)}${chip(SCOPE[det.impact][0], SCOPE[det.impact][1])}
      <span class="small muted">${f.status === "aborted" ? `fault stopped early by ${det.abort_kind === "threshold" ? "the safety threshold" : "the kill switch"}` : ""}</span></div>
    <div class="fcols">
      <div><h3>What broke <span class="tag">measured</span></h3>
        <ul class="plain">${li(names(det.failed), "critical", "Failed")}${li(names(det.degraded), "warning", "Degraded")}${li(names(det.functional), "good", "Unaffected")}</ul>
        <div class="small">${det.recovery_verified ? chip(recoveryText(det) + " after rollback", "good") : det.injected ? chip("Recovery NOT verified", "critical") : ""}</div></div>
      <div><h3>Likely cause</h3>${rc ? `<p>${esc(humanize(rc.hypothesis))}</p><div class="small muted">${tag} ${esc(rc.confidence)} confidence</div>` : `<p><b>Inconclusive.</b> ${esc(d.summary)}</p>`}</div>
      <div><h3>Recommended fix</h3><p>${esc(humanize(d.recommendation.action))}</p>
        ${d.verification.success_criteria.length ? `<div class="small muted">Fixed when: ${esc(d.verification.success_criteria.join("; "))}</div>` : ""}</div>
    </div>
    <details class="small"><summary class="muted">Evidence (${det.facts.length} measured facts) and limitations</summary>
      ${rc ? `<div><b>Supports the cause:</b></div><ul>${rc.supporting_evidence.map((i) => `<li class="fact"><b>${esc(i)}</b> ${esc(facts[i] || "?")}</li>`).join("")}</ul>` : ""}
      ${rc && rc.contradicting_evidence.length ? `<div><b>Speaks against it:</b></div><ul>${rc.contradicting_evidence.map((i) => `<li class="fact"><b>${esc(i)}</b> ${esc(facts[i] || "?")}</li>`).join("")}</ul>` : ""}
      ${d.root_causes.slice(1).map((r) => `<div>${tag} ${esc(r.hypothesis)} <span class="muted">(${esc(r.confidence)})</span></div>`).join("")}
      <div><b>All measured facts:</b></div><ul>${det.facts.map((x) => `<li class="fact"><b>${esc(x.id)}</b> ${esc(x.text)}</li>`).join("")}</ul>
      <ul>${[...new Set([...det.limitations, ...d.limitations])].map((x) => `<li class="muted">${esc(x)}</li>`).join("")}</ul>
      ${f.retest ? `<div>Change since previous run <code>${esc(f.retest.previous_run)}</code>: severity ${f.retest.severity_delta ?? "n/a"}, resilience ${f.retest.resilience_delta ?? "n/a"}.</div>` : ""}
      <code>${esc(f.run_id)}</code> · <a href="/api/runs/${encodeURIComponent(f.run_id)}/export" target="_blank">evidence JSON ↗</a> · <a href="/api/runs/${encodeURIComponent(f.run_id)}/report" target="_blank">run report ↗</a>
    </details></div>`;
}
async function loadFindings() {
  let d;
  try { d = await api("/api/findings"); } catch (e) { $("#fd-note").textContent = "Findings unavailable: " + e.message; return; }
  const changed = JSON.stringify(d) !== JSON.stringify(findingsData);
  findingsData = d;
  renderScore();
  if (!changed && $("#fd-score").children.length) return;   // unchanged: keep open details as they are
  const o = d.overall, pct = (v) => (v == null ? null : Math.round(v * 100));
  const meter = (label, v, hint) => `<div class="meter"><div class="row between"><span>${label}</span><b>${v == null ? "n/a" : v + "%"}</b></div><div class="track"><div class="fill" style="width:${v ?? 0}%"></div></div><div class="small muted">${hint}</div></div>`;
  $("#fd-score").innerHTML = `<h2>Resilience score</h2>
    <div class="bigscore">${o.score == null ? `<span class="muted">n/a</span>` : `${Math.round(o.score)}<span class="of"> / 100</span>`}</div>
    <div class="small">${o.scenarios_scored} of ${o.scenarios_total} failure scenarios tested · ${o.experiments_analysed} experiments${o.capped_unrecovered ? " · <b>capped at 25: a run did not recover</b>" : ""}</div>
    ${meter("Stays available during a fault", pct(o.components.availability), "half of the score")}
    ${meter("Stays fast during a fault", pct(o.components.performance), "20% of the score")}
    ${meter("Recovers quickly afterwards", pct(o.components.recovery), "30% of the score")}
    <div class="small muted">Computed from measurements only, never by the AI.</div>`;
  const chosen = new Set(o.per_scenario.map((s) => s.run_id));
  const main = d.findings.filter((f) => chosen.has(f.run_id) && f.severity.score != null), other = d.findings.filter((f) => !main.includes(f));
  rows($("#fd-summary tbody"), main.map((f, i) => { const det = f.deterministic; return `<tr><td><b>${i + 1}</b></td><td><b>${esc(faultTitle(det))}</b></td><td>${sevChip(f.severity)}</td><td>${chip(SCOPE[det.impact][0], SCOPE[det.impact][1])} <span class="small muted">${det.failed.length} failed, ${det.degraded.length} degraded of ${det.workflows.filter((w) => w.status !== "no_data").length} workflows</span></td><td>${esc(recoveryText(det))}</td></tr>`; }), 5, "No scored experiment yet.");
  $("#fd-note").textContent = (o.untested_scenarios.length ? "Not yet tested: " + o.untested_scenarios.map((s) => faultTitle({ fault_type: s[0], target: s[1] })).join(", ") + ". " : "") + "The score covers only these scenarios, not every possible failure.";
  const rules = main.filter((f) => f.source.mode !== "llm"), err = rules.find((f) => f.source.error);
  $("#fd-notice").hidden = !rules.length;
  $("#fd-notice").textContent = rules.length ? `The likely causes and fixes below (${rules.length} of ${main.length}) come from deterministic rules, not an AI model` + (err ? `: the LLM provider failed (${err.source.error})` : ": no LLM provider is configured") + ". What broke, the severity and the score are measured and do not depend on the AI." : "";
  $("#fd-empty").hidden = d.findings.length > 0;
  $("#fd-list").innerHTML = main.map((f, i) => findingHtml(f, i + 1)).join("");
  $("#fd-other-wrap").hidden = !other.length;
  $("#fd-other").innerHTML = other.map((f) => findingHtml(f, 0)).join("");
  $("#fd-method").textContent = d.methodology;
}
$("#fd-analyze-all").addEventListener("click", async (ev) => {
  ev.target.disabled = true;
  try { const r = await api("/api/analyze-all", { method: "POST" }); toast(r.analysed.length ? `Analysed ${r.analysed.length} run(s)` : "Nothing new to analyse"); }
  catch (e) { toast("Analysis failed: " + e.message); }
  ev.target.disabled = false; loadFindings();
});
async function loadAi() {
  try {
    const a = await api("/api/ai/status");
    $("#ai-pill").innerHTML = a.provider ? chip(`AI: ${a.provider}:${a.model}`, "good") : chip("No AI model · using rules", "warning");
    $("#ai-pill").title = a.provider ? "Plans and diagnoses are requested from this model, then validated" : "No LLM provider is configured. Plans and diagnoses come from deterministic rules and are labelled as such.";
  } catch { /* the connection pill already shows the problem */ }
}

// ---------------------------------------------------------------- refresh loop
async function tick() {
  let v;
  try { v = await getView(); } catch { return; }
  lastView = v;
  if (currentTab === "overview") {
    renderHero(v);
    renderStory($("#story-ov"), story(v));
    renderWorkflows($("#wf-ov"), v);
    renderChart(v);
    renderLog(v);
  }
  if (currentTab === "experiments") {
    const st = statusOf(v);
    $("#now-title").textContent = !v || v.source === "other" ? "No experiment yet" : `${v.busy ? "Running now" : "Latest experiment"}: ${faultTitle(v.config)}`;
    $("#now-status").innerHTML = chip(st.label, st.tone);
    renderStory($("#story-ex"), story(v));
  }
}
async function refreshAll() {
  await loadOverview().catch(() => {});
  await tick();
  await loadFindings();
  if (currentTab === "overview") { loadGraph(); tick(); }
  if (currentTab === "experiments") { loadPlan(); loadRuns(); }
}
(async function init() {
  legend($("#lg-ov"));
  await loadCatalog();
  loadAi();
  const wanted = location.hash.slice(1);
  if (["experiments", "findings"].includes(wanted)) showTab(wanted); else await refreshAll();
  setInterval(tick, 1000);
  setInterval(() => loadOverview().catch(() => {}), 3000);
  setInterval(() => { if (currentTab === "overview") loadGraph(); if (currentTab === "experiments") { loadPlan(); loadRuns(); } }, 3000);
  setInterval(loadFindings, 5000);
  setInterval(loadAi, 30000);
})();
