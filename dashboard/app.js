"use strict";
// Paperless Chaos Lab dashboard. Every control calls the real backend; when
// data is missing or the backend is unreachable we show that, never a stand-in.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "–");
const fmtMs = (v) => (v == null ? "–" : v >= 1000 ? (v / 1000).toFixed(2) + "s" : Math.round(v) + "ms");
const fmtPct = (v) => (v == null ? "–" : (v * 100).toFixed(v > 0 && v < 0.1 ? 1 : 0) + "%");
const PROBES = ["web_root", "api_documents", "api_status", "doc_upload"];
const PROBE_VAR = { web_root: "--s1", api_documents: "--s2", api_status: "--s3", doc_upload: "--s4" };
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const STEPS = ["preflight", "baseline", "injecting", "active", "rolling_back", "verifying_recovery", "observing", "done"];
const TERMINAL = ["completed", "aborted", "failed", "rejected", "interrupted"];

let catalog = null, currentTab = "overview", selectedRun = null, lastLive = null, graphData = null;

// ---------------------------------------------------------------- utilities
async function api(path, opts = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...opts };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    setConn(false);
    throw new Error("controller unreachable");
  }
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
function setConn(ok) {
  const el = $("#conn");
  el.textContent = ok ? "● controller connected" : "● controller unreachable";
  el.className = "pill " + (ok ? "st-good" : "st-critical");
}
let toastTimer;
function toast(msg, ms = 5000) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), ms);
}
function statusClass(s) {
  s = (s || "").toLowerCase();
  if (["healthy", "running", "ok", "completed", "pass", "true", "external"].includes(s)) return "st-good";
  if (["starting", "baseline", "preflight", "observing", "verifying_recovery", "aborted"].includes(s)) return "st-warning";
  if (["unhealthy", "injecting", "active", "rolling_back"].includes(s)) return "st-serious";
  if (["paused", "exited", "dead", "missing", "failed", "fail", "false", "error", "rejected", "interrupted", "stopped"].includes(s)) return "st-critical";
  return "st-unknown";
}
const badge = (label, cls) => `<span class="status ${cls || statusClass(label)}">${esc(label)}</span>`;
const okBadge = (ok, yes = "PASS", no = "FAIL") => (ok == null ? badge("n/a", "st-unknown") : badge(ok ? yes : no, ok ? "st-good" : "st-critical"));
function rows(tbody, html, emptyCols, emptyMsg) {
  tbody.innerHTML = html.length ? html.join("") : `<tr><td colspan="${emptyCols}" class="muted">${esc(emptyMsg)}</td></tr>`;
}

// ---------------------------------------------------------------- tabs
$$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  currentTab = name;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "graph") loadGraph();
  if (name === "obs") loadRuns(true);
}

// ---------------------------------------------------------------- kill switch + unblock
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

// ---------------------------------------------------------------- overview
async function loadOverview() {
  let d;
  try { d = await api("/api/overview"); } catch (e) { renderOverviewError(e); return; }
  const m = d.monitor || {};
  const app = $("#ov-app");
  if (!m.ts) {
    app.querySelector(".big").innerHTML = badge("waiting", "st-unknown");
    app.querySelector(".sub").textContent = "monitor has not sampled yet";
  } else {
    app.querySelector(".big").innerHTML = m.app_ok ? badge("Healthy", "st-good") : badge("Degraded", "st-critical");
    app.querySelector(".sub").textContent = (m.probes || []).map((p) => `${p.probe} ${p.ok ? "ok" : "FAIL"} ${fmtMs(p.latency_ms)}`).join(" · ") + ` · ${fmtTime(m.ts)}`;
  }
  const svcs = m.services || [];
  const deps = $("#ov-deps");
  if (m.docker_error) {
    deps.querySelector(".big").innerHTML = badge("Docker unreachable", "st-critical");
    deps.querySelector(".sub").textContent = m.docker_error;
  } else if (!svcs.length) {
    deps.querySelector(".big").innerHTML = badge(m.ts ? "No containers" : "waiting", m.ts ? "st-critical" : "st-unknown");
    deps.querySelector(".sub").textContent = m.ts ? "sandbox not running: python -m chaos up" : "";
  } else {
    const bad = svcs.filter((s) => !s.ok);
    deps.querySelector(".big").innerHTML = bad.length ? badge(`${bad.length} of ${svcs.length} unhealthy`, "st-critical") : badge(`${svcs.length}/${svcs.length} healthy`, "st-good");
    deps.querySelector(".sub").textContent = svcs.map((s) => `${s.service}: ${s.state}/${s.health}`).join(" · ");
  }
  rows($("#ov-services tbody"), svcs.map((s) => `<tr><td>${esc(s.service)}</td><td>${badge(s.state)}</td><td>${badge(s.health)}</td><td><code>${esc(s.id)}</code></td></tr>`), 4, m.docker_error || "no data yet");
  $("#ov-svc-ts").textContent = m.ts ? "as of " + fmtTime(m.ts) : "";
  rows($("#ov-probes tbody"), (m.probes || []).map((p) => `<tr><td>${esc(p.probe)}</td><td>${okBadge(p.ok, "OK", "FAIL")}</td><td class="num">${fmtMs(p.latency_ms)}</td><td class="small">${esc(p.detail)}</td></tr>`), 4, "no probes yet");

  const ex = d.experiment || {}; const live = ex.live || {};
  const exEl = $("#ov-exp");
  if (ex.busy) {
    exEl.querySelector(".big").innerHTML = badge(live.state || "running", statusClass(live.state));
    exEl.querySelector(".sub").textContent = `${live.kind || ""} ${live.run_id || ""} · ${live.message || ""}`;
  } else {
    exEl.querySelector(".big").innerHTML = badge("Idle", "st-good");
    exEl.querySelector(".sub").textContent = live.run_id ? `last: ${live.run_id} → ${live.result || live.state}` : "no experiment run in this controller session";
  }
  renderSafety(d.safety);
  renderTopState(ex);

  const b = d.baseline;
  $("#ov-base-empty").hidden = !!b;
  $("#ov-baseline").hidden = !b;
  if (b) {
    $("#ov-base-ts").textContent = `${b.id} · ${fmtTime(b.created)}`;
    const probes = ((b.summary || {}).metrics || {}).baseline?.probes || {};
    rows($("#ov-baseline tbody"), Object.entries(probes).map(([k, s]) => `<tr><td>${esc(k)}</td><td class="num">${s.n}</td><td class="num">${fmtPct(s.error_rate)}</td><td class="num">${fmtMs(s.p50_ms)}</td><td class="num">${fmtMs(s.p95_ms)}</td></tr>`), 5, "baseline has no samples");
  }
  if (catalog) $("#wl-desc").textContent = `${catalog.workload.version}: ` + Object.entries(catalog.workload.intervals_s).map(([k, v]) => `${k} every ${v}s`).join(", ") + ", plus document processing tracked per upload.";
}
function renderOverviewError(e) {
  ["#ov-app", "#ov-deps", "#ov-exp", "#ov-safety"].forEach((s) => {
    $(s + " .big").innerHTML = badge("unavailable", "st-critical");
    $(s + " .sub").textContent = e.message;
  });
}
function renderSafety(s) {
  if (!s) return;
  const el = $("#ov-safety");
  const blocked = s.blocked;
  $("#blocked-banner").hidden = !blocked;
  if (blocked) $("#blocked-reason").textContent = `${blocked.reason} (run ${blocked.run_id || "–"}, ${fmtTime(blocked.ts)})`;
  el.querySelector(".big").innerHTML = blocked ? badge("BLOCKED", "st-critical") : s.active_fault ? badge("Fault active", "st-serious") : badge("Armed", "st-good");
  el.querySelector(".sub").textContent = s.active_fault ? `${s.active_fault.fault_type} on ${s.active_fault.service}` : "allowlist + thresholds + watchdog + kill switch";
  rows($("#ov-events tbody"), (s.events || []).slice(0, 15).map((e) => `<tr><td>${fmtTime(e.ts)}</td><td>${badge(e.kind, e.level === "error" ? "st-critical" : e.level === "warning" ? "st-warning" : "st-good")}</td><td class="small"><code>${esc(e.run_id || "")}</code></td><td class="small">${esc(e.message)}</td></tr>`), 4, "no safety events yet");
}
function renderTopState(ex) {
  const live = ex.live || {};
  const el = $("#top-exp");
  if (ex.busy) { el.innerHTML = `${esc(live.kind || "run")}: <b>${esc(live.state)}</b>`; el.className = "pill " + statusClass(live.state); }
  else { el.textContent = "no experiment running"; el.className = "pill"; }
}
$("#run-baseline").addEventListener("click", async () => {
  try {
    const r = await api("/api/baseline", { method: "POST", body: { duration_s: +$("#base-dur").value } });
    toast("Baseline started: " + r.run_id);
  } catch (e) { toast("Baseline rejected: " + e.message); }
  refreshAll();
});

async function loadMonitorChart() {
  try {
    const d = await api("/api/monitor?minutes=15");
    const el = $("#ov-monitor-chart");
    if (!d.samples.length) { el.innerHTML = `<div class="empty">No monitor samples yet.</div>`; return; }
    latencyChart(el, d.samples, { probes: ["web_root", "api_status"] });
  } catch (e) { $("#ov-monitor-chart").innerHTML = `<div class="empty">Monitor data unavailable: ${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- charts
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
function bands(svg, sx, opts, top, h) {
  let s = "";
  const band = (a, b, fill, label) => {
    if (!a) return "";
    const x0 = sx(a), x1 = sx(b || opts.tEnd);
    return `<rect x="${x0}" y="${top}" width="${Math.max(1, x1 - x0)}" height="${h}" fill="var(${fill})"/><text x="${x0 + 4}" y="${top + 12}" font-size="11" fill="var(--text-2)">${label}</text>`;
  };
  s += band(opts.faultStart, opts.faultEnd, "--fault-band", "fault");
  if (opts.faultEnd && opts.recoveredAt) s += band(opts.faultEnd, opts.recoveredAt, "--recovery-band", "recovering");
  return s;
}
function latencyChart(el, samples, opts = {}) {
  const probes = opts.probes || PROBES;
  const data = samples.filter((s) => probes.includes(s.probe));
  if (!data.length) { el.innerHTML = `<div class="empty">No latency samples for this run.</div>`; return; }
  const W = 960, H = opts.height || 260, m = { l: 56, r: 16, t: 10, b: 28 };
  const t0 = Math.min(...data.map((s) => s.ts), opts.faultStart || Infinity);
  const t1 = Math.max(...data.map((s) => s.ts), opts.faultEnd || 0, opts.now || 0);
  const ymax = niceMax(Math.max(...data.map((s) => s.latency_ms || 0)) * 1.08);
  const sx = (t) => m.l + ((t - t0) / Math.max(1, t1 - t0)) * (W - m.l - m.r);
  const sy = (v) => H - m.b - (v / ymax) * (H - m.t - m.b);
  let g = bands(null, sx, { ...opts, tEnd: t1 }, m.t, H - m.t - m.b);
  for (let i = 0; i <= 4; i++) {
    const v = (ymax / 4) * i, y = sy(v);
    g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${fmtMs(v)}</text>`;
  }
  for (const t of timeTicks(t0, t1)) g += `<text x="${sx(t)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="var(--muted)">${new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</text>`;
  for (const p of probes) {
    const pts = data.filter((s) => s.probe === p).sort((a, b) => a.ts - b.ts);
    if (!pts.length) continue;
    const color = `var(${PROBE_VAR[p]})`;
    g += `<polyline fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" points="${pts.map((s) => `${sx(s.ts).toFixed(1)},${sy(s.latency_ms || 0).toFixed(1)}`).join(" ")}"/>`;
    for (const s of pts.filter((s) => !s.ok)) {
      const x = sx(s.ts), y = sy(s.latency_ms || 0);
      g += `<path d="M${x - 4},${y - 4}L${x + 4},${y + 4}M${x - 4},${y + 4}L${x + 4},${y - 4}" stroke="var(--critical)" stroke-width="2"/>`;
    }
  }
  g += `<line class="xh" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" stroke="var(--muted)" stroke-dasharray="3 3" visibility="hidden"/>`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="latency per probe over time">${g}<rect x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent" class="hit"/></svg>`;
  const svg = el.querySelector("svg"), xh = svg.querySelector(".xh");
  svg.querySelector(".hit").addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect(), x = ((ev.clientX - r.left) / r.width) * W;
    const t = t0 + ((x - m.l) / (W - m.l - m.r)) * (t1 - t0);
    const near = {};
    for (const s of data) if (!near[s.probe] || Math.abs(s.ts - t) < Math.abs(near[s.probe].ts - t)) near[s.probe] = s;
    xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.setAttribute("visibility", "visible");
    showTip(ev, `${new Date(t * 1000).toLocaleTimeString()}\n` + probes.filter((p) => near[p]).map((p) => `${p}: ${fmtMs(near[p].latency_ms)} ${near[p].ok ? "ok" : "✕ " + (near[p].detail || "")} [${near[p].phase}]`).join("\n"));
  });
  svg.querySelector(".hit").addEventListener("mouseleave", () => { hideTip(); xh.setAttribute("visibility", "hidden"); });
}
function errorChart(el, samples, opts = {}) {
  const data = samples.filter((s) => PROBES.includes(s.probe));
  if (!data.length) { el.innerHTML = `<div class="empty">No samples.</div>`; return; }
  const W = 960, H = 200, m = { l: 44, r: 12, t: 10, b: 26 }, B = 5;
  const t0 = Math.floor(Math.min(...data.map((s) => s.ts)) / B) * B, t1 = Math.max(...data.map((s) => s.ts));
  const buckets = [];
  for (let t = t0; t <= t1; t += B) {
    const inb = data.filter((s) => s.ts >= t && s.ts < t + B);
    if (inb.length) buckets.push({ t, n: inb.length, err: inb.filter((s) => !s.ok).length });
  }
  const sx = (t) => m.l + ((t - t0) / Math.max(1, t1 + B - t0)) * (W - m.l - m.r);
  const sy = (v) => H - m.b - v * (H - m.t - m.b);
  let g = bands(null, sx, { ...opts, tEnd: t1 + B }, m.t, H - m.t - m.b);
  for (const v of [0, 0.25, 0.5, 0.75, 1]) g += `<line x1="${m.l}" x2="${W - m.r}" y1="${sy(v)}" y2="${sy(v)}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${sy(v) + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${v * 100}%</text>`;
  if (opts.threshold != null) g += `<line x1="${m.l}" x2="${W - m.r}" y1="${sy(opts.threshold)}" y2="${sy(opts.threshold)}" stroke="var(--critical)" stroke-dasharray="5 4"/><text x="${W - m.r}" y="${sy(opts.threshold) - 4}" text-anchor="end" font-size="11" fill="var(--text-2)">abort threshold ${fmtPct(opts.threshold)}</text>`;
  const bw = Math.max(2, sx(t0 + B) - sx(t0) - 2);
  for (const b of buckets) {
    const rate = b.err / b.n, y = sy(rate);
    g += `<rect x="${sx(b.t) + 1}" y="${Math.min(y, H - m.b - 1)}" width="${bw}" height="${Math.max(1, H - m.b - y)}" rx="2" fill="var(--s1)"><title>${new Date(b.t * 1000).toLocaleTimeString()}: ${b.err}/${b.n} failed (${fmtPct(rate)})</title></rect>`;
  }
  for (const t of timeTicks(t0, t1)) g += `<text x="${sx(t)}" y="${H - 7}" text-anchor="middle" font-size="11" fill="var(--muted)">${new Date(t * 1000).toLocaleTimeString([], { minute: "2-digit", second: "2-digit" })}</text>`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="error rate per 5 second bucket">${g}</svg>`;
}
function showTip(ev, text) { const t = $("#tip"); t.textContent = text; t.hidden = false; t.style.left = Math.min(ev.clientX + 14, innerWidth - 380) + "px"; t.style.top = ev.clientY + 14 + "px"; }
function hideTip() { $("#tip").hidden = true; }
function legend(el, probes = PROBES) {
  el.innerHTML = probes.map((p) => `<span><span class="sw" style="background:var(${PROBE_VAR[p]})"></span>${p}</span>`).join("") + `<span><b style="color:var(--critical)">✕</b> failed probe</span><span><span class="sw" style="background:var(--fault-band);height:10px"></span>fault window</span><span><span class="sw" style="background:var(--recovery-band);height:10px"></span>recovering</span>`;
}

// ---------------------------------------------------------------- dependency graph
async function loadGraph() {
  try { graphData = await api("/api/graph"); } catch (e) { $("#g-svg").innerHTML = `<div class="empty">Discovery failed: ${esc(e.message)}</div>`; return; }
  renderGraph(graphData);
}
$("#g-refresh").addEventListener("click", loadGraph);
function renderGraph(g) {
  $("#g-src").textContent = `compose file + ${g.source.runtime || "NO runtime data"} · ${fmtTime(g.generated)}`;
  $("#g-error").hidden = !g.source.runtime_error;
  $("#g-error").textContent = g.source.runtime_error ? "Runtime state unavailable (statuses unknown): " + g.source.runtime_error : "";
  $("#g-legend").innerHTML = `<span><span class="sw" style="background:var(--text-2)"></span>declared (depends_on)</span><span><svg width="22" height="6"><line x1="0" y1="3" x2="22" y2="3" stroke="var(--text-2)" stroke-width="2" stroke-dasharray="5 3"/></svg> inferred only (env / ports)</span><span><svg width="22" height="6"><line x1="0" y1="3" x2="22" y2="3" stroke="var(--accent)" stroke-width="2" stroke-dasharray="1 3"/></svg> manual</span><span>🎯 approved chaos target</span>`;
  // layered layout: depth = longest path from a node with no incoming edges
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
  const NW = 230, NH = 92, GX = 110, GY = 28;
  const nl = Object.keys(layers).length, maxRows = Math.max(...Object.values(layers).map((l) => l.length));
  const W = nl * NW + (nl - 1) * GX + 40, H = maxRows * NH + (maxRows - 1) * GY + 40;
  const pos = {};
  Object.entries(layers).forEach(([L, ns]) => ns.forEach((n, i) => {
    const off = (H - (ns.length * NH + (ns.length - 1) * GY)) / 2;
    pos[n.id] = { x: 20 + L * (NW + GX), y: off + i * (NH + GY) };
  }));
  let s = `<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0L10,5L0,10z" fill="var(--text-2)"/></marker></defs>`;
  for (const e of g.edges) {
    const a = pos[e.from], b = pos[e.to]; if (!a || !b) continue;
    const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
    const manual = e.kinds.includes("manual") && e.kinds.length === 1, declared = e.kinds.includes("declared");
    const dash = declared ? "" : manual ? "1 4" : "6 4";
    s += `<path d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 2},${y2}" fill="none" stroke="${manual ? "var(--accent)" : "var(--text-2)"}" stroke-width="2" stroke-dasharray="${dash}" marker-end="url(#arr)"><title>${esc(e.from)} → ${esc(e.to)}\n${esc(e.evidence.join("\n"))}</title></path>`;
    s += `<text x="${mx}" y="${(y1 + y2) / 2 - 6}" text-anchor="middle" font-size="11" fill="var(--text-2)">${esc(e.kinds.join("+"))}</text>`;
  }
  for (const n of g.nodes) {
    const p = pos[n.id], cls = statusClass(n.status);
    const stroke = { "st-good": "var(--good)", "st-warning": "var(--warning)", "st-serious": "var(--serious)", "st-critical": "var(--critical)" }[cls] || "var(--border)";
    const icon = { "st-good": "✓", "st-warning": "…", "st-serious": "!", "st-critical": "✕" }[cls] || "?";
    s += `<g><rect x="${p.x}" y="${p.y}" width="${NW}" height="${NH}" rx="10" fill="var(--surface)" stroke="${stroke}" stroke-width="${cls === "st-good" ? 1.5 : 3}"><title>${esc(JSON.stringify(n.container || n.note || n.id, null, 1))}</title></rect>
      <text x="${p.x + 12}" y="${p.y + 22}" font-size="14" font-weight="700" fill="var(--text)">${esc(n.id)}${n.chaos_target ? " 🎯" : ""}${n.declared === false && n.kind !== "external" ? " (undeclared)" : ""}</text>
      <text x="${p.x + 12}" y="${p.y + 41}" font-size="12" font-weight="600" fill="${stroke === "var(--border)" ? "var(--muted)" : stroke}">${icon} ${esc(n.status)}</text>
      <text x="${p.x + 12}" y="${p.y + 58}" font-size="11" fill="var(--muted)">${esc((n.image || n.note || "").slice(0, 34))}</text>
      <text x="${p.x + 12}" y="${p.y + 75}" font-size="11" fill="var(--text-2)">${esc(n.healthcheck ? "hc: " + String(n.healthcheck).slice(0, 30) : n.note ? n.note.slice(0, 34) : "")}</text></g>`;
  }
  $("#g-svg").innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="min-width:${Math.min(W, 760)}px">${s}</svg>`;
  rows($("#g-edges tbody"), g.edges.map((e) => `<tr><td>${esc(e.from)} → ${esc(e.to)}</td><td>${esc(e.kinds.join(", "))}</td><td class="small">${esc(e.evidence.join("; "))}</td></tr>`), 3, "no edges");
  rows($("#g-suppressed tbody"), g.suppressed_edges.map((e) => `<tr><td>${esc(e.from)} → ${esc(e.to)}</td><td class="small">${esc(e.kinds.join(", "))}</td><td><button data-undo="remove_edges" data-src="${esc(e.from)}" data-dst="${esc(e.to)}">restore</button></td></tr>`), 3, "none");
  const ov = g.overrides || {};
  const orows = [
    ...(ov.add_edges || []).map((e) => `<tr><td>+ ${esc(e.from)} → ${esc(e.to)}</td><td class="small">${esc(e.note)}</td><td><button data-undo="add_edges" data-src="${esc(e.from)}" data-dst="${esc(e.to)}">undo</button></td></tr>`),
    ...(ov.remove_edges || []).map((e) => `<tr><td>− ${esc(e.from)} → ${esc(e.to)}</td><td class="small">${esc(e.note)}</td><td><button data-undo="remove_edges" data-src="${esc(e.from)}" data-dst="${esc(e.to)}">undo</button></td></tr>`),
    ...Object.entries(ov.notes || {}).map(([k, v]) => `<tr><td>note on ${esc(k)}</td><td class="small">${esc(v)}</td><td><button data-undo="notes" data-node="${esc(k)}">undo</button></td></tr>`),
  ];
  rows($("#g-overrides tbody"), orows, 3, "no manual corrections");
  const opts = g.nodes.map((n) => `<option>${esc(n.id)}</option>`).join("");
  const f = $("#g-form");
  ["from", "to", "node"].forEach((k) => { const v = f[k].value; f[k].innerHTML = opts; if (v) f[k].value = v; });
}
$("#g-form").action.addEventListener("change", (e) => {
  const note = e.target.value === "note";
  $$(".edge-only").forEach((x) => (x.hidden = note)); $$(".note-only").forEach((x) => (x.hidden = !note));
});
$("#g-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, body = { action: f.action.value, note: f.note.value };
  if (body.action === "note") body.node = f.node.value; else { body.from = f.from.value; body.to = f.to.value; }
  try { graphData = await api("/api/graph/overrides", { method: "POST", body }); renderGraph(graphData); f.note.value = ""; toast("Correction saved."); }
  catch (err) { toast("Correction rejected: " + err.message); }
});
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-undo]"); if (!b) return;
  const q = new URLSearchParams({ kind: b.dataset.undo, src: b.dataset.src || "", dst: b.dataset.dst || "", node: b.dataset.node || "" });
  try { graphData = await api("/api/graph/overrides?" + q, { method: "DELETE" }); renderGraph(graphData); } catch (err) { toast(err.message); }
});

// ---------------------------------------------------------------- experiment lab
async function loadCatalog() {
  try { catalog = await api("/api/catalog"); } catch (e) { toast("Cannot load catalog: " + e.message); return; }
  const f = $("#exp-form");
  f.fault_type.innerHTML = Object.entries(catalog.faults).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  const L = catalog.limits;
  f.duration_s.min = L.min_duration_s; f.duration_s.max = L.max_duration_s;
  f.latency_ms.min = L.min_latency_ms; f.latency_ms.max = L.max_latency_ms;
  $("#limits-desc").textContent = `Safety limits: duration ${L.min_duration_s}–${L.max_duration_s}s; latency ${L.min_latency_ms}–${L.max_latency_ms}ms; latency threshold ${L.threshold_latency_ms.join("–")}ms; window ${L.threshold_window_s.join("–")}s; recovery ≤ ${L.max_recovery_timeout_s}s; independent watchdog removes the fault ${L.watchdog_grace_s}s after the planned end if the controller has not. Approved targets: ${Object.entries(catalog.targets).map(([k, v]) => `${k} (${v.join(", ")})`).join("; ")}.`;
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
  $("#preflight").innerHTML = (checks || []).map((c) => `<li>${okBadge(c.ok)} ${esc(c.name)} <span class="muted">${esc(c.detail)}</span></li>`).join("") || `<li class="muted">no checks returned</li>`;
  const box = $("#exp-errors");
  box.hidden = !(errors && errors.length);
  box.innerHTML = (errors || []).map((e) => `<div>⚠ ${esc(e)}</div>`).join("");
}
$("#btn-preflight").addEventListener("click", async () => {
  $("#preflight").innerHTML = `<li class="muted">running…</li>`;
  try { const r = await api("/api/experiments/preflight", { method: "POST", body: readConfig() }); renderChecks(r.checks, r.errors); }
  catch (e) { renderChecks(e.data?.checks || [], [e.message]); }
});
$("#exp-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#btn-start").disabled = true;
  try {
    const r = await api("/api/experiments", { method: "POST", body: readConfig() });
    toast("Experiment started: " + r.run_id); renderChecks([], []); selectedRun = r.run_id;
  } catch (err) { renderChecks(err.data?.checks || [], err.data?.errors || [err.message]); }
  finally { $("#btn-start").disabled = false; pollLive(); }
});

async function pollLive() {
  let d;
  try { d = await api("/api/experiments/current"); } catch { return; }
  renderTopState(d);
  const live = d.live || {};
  lastLive = live;
  $("#btn-start").disabled = d.busy;
  $("#live-id").textContent = live.run_id ? `${live.run_id} · ${live.kind || ""}` : "";
  const st = live.state || "";
  const idx = st === "done" || TERMINAL.includes(st) ? STEPS.length - 1 : STEPS.indexOf(st);
  const failed = ["failed", "aborted", "rejected", "interrupted"].includes(st);
  $("#pipeline").innerHTML = live.kind === "experiment" ? STEPS.map((s, i) => `<li class="${i < idx ? "done" : i === idx ? (failed && i === STEPS.length - 1 ? "bad" : "now") : ""}">${s === "done" ? (TERMINAL.includes(st) ? st : "done") : s.replace("_", " ")}</li>`).join("") : live.run_id ? `<li class="now">baseline workload: ${esc(st)}</li>` : `<li>no experiment in this controller session yet</li>`;
  const samples = live.samples || [];
  const now = Date.now() / 1000;
  const win = (live.config?.thresholds?.window_s) || 10;
  const recent = samples.filter((s) => PROBES.includes(s.probe) && s.ts >= now - win);
  const err = recent.length ? recent.filter((s) => !s.ok).length / recent.length : null;
  const lat = recent.map((s) => s.latency_ms).sort((a, b) => a - b);
  const p95 = lat.length ? lat[Math.max(0, Math.ceil(lat.length * 0.95) - 1)] : null;
  const faultSecs = live.fault_start ? ((live.fault_end || now) - live.fault_start) : null;
  $("#live-stats").innerHTML = [
    ["state", st || "idle"], ["phase", live.phase || "–"], ["target", live.config?.target || "–"],
    [`error rate (last ${win}s)`, fmtPct(err)], [`p95 (last ${win}s)`, fmtMs(p95)], ["fault active for", faultSecs == null ? "–" : faultSecs.toFixed(1) + "s"],
  ].map(([k, v]) => `<div class="mini"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("");
  $("#live-countdown").textContent = live.planned_end && !live.fault_end ? `Fault auto-removes in ${Math.max(0, live.planned_end - now).toFixed(0)}s at the latest (planned end). ${live.message || ""}` : (live.message || "");
  const br = live.breaches || [];
  $("#live-breach").hidden = !br.length;
  $("#live-breach").innerHTML = br.map((b) => `⚡ Automatic abort: ${esc(b.message)}`).join("<br>");
  if (samples.length) latencyChart($("#live-chart"), samples, { faultStart: live.fault_start, faultEnd: live.fault_end, now: d.busy ? now : null, height: 200 });
  else $("#live-chart").innerHTML = `<div class="empty">${live.run_id ? "waiting for samples…" : "Start an experiment to see live data."}</div>`;
  rows($("#live-events tbody"), (live.events || []).slice(0, 20).map((e) => `<tr><td>${fmtTime(e.ts)}</td><td>${badge(e.kind, e.level === "error" ? "st-critical" : e.level === "warning" ? "st-warning" : "st-good")}</td><td class="small">${esc(e.message)}</td></tr>`), 3, "no events");
  if (currentTab === "obs" && selectedRun && selectedRun === live.run_id && d.busy) loadRun(selectedRun);
}

// ---------------------------------------------------------------- observability
async function loadRuns(selectLatest) {
  let runs;
  try { runs = await api("/api/runs?limit=40"); } catch (e) { $("#obs-empty").hidden = false; $("#obs-empty").textContent = "Cannot load runs: " + e.message; return; }
  const sel = $("#obs-run");
  $("#obs-empty").hidden = runs.length > 0;
  sel.innerHTML = runs.map((r) => `<option value="${r.id}">${r.id} · ${r.kind} · ${r.state}${r.config?.target ? " · " + r.config.target : ""}${r.result ? " · " + r.result : ""}</option>`).join("");
  if (!runs.length) return;
  if (!selectedRun || !runs.find((r) => r.id === selectedRun) || (selectLatest && !selectedRun)) {
    selectedRun = (runs.find((r) => r.kind === "experiment" && r.state !== "rejected") || runs[0]).id;
  }
  sel.value = selectedRun;
  loadRun(selectedRun);
}
$("#obs-run").addEventListener("change", (e) => { selectedRun = e.target.value; loadRun(selectedRun); });
async function loadRun(id) {
  let r;
  try { r = await api("/api/runs/" + encodeURIComponent(id)); } catch (e) { toast("Cannot load run: " + e.message); return; }
  const o = r.outcome || {}, metrics = (r.summary || {}).metrics, rec = o.recovery || {}, rem = o.fault_removal || {};
  $("#obs-report").href = r.kind === "experiment" ? `/api/runs/${encodeURIComponent(id)}/report` : "#";
  $("#obs-report").hidden = r.kind !== "experiment";
  const recoveredAt = rec.recovery_time_s != null && r.fault_end ? r.fault_end + rec.recovery_time_s : null;
  const cards = [
    ["State", badge(r.state), `${r.kind} · ${esc(o.result || "in progress")}`],
    ["Target & fault", esc(r.config?.target || "–"), esc(`${r.config?.fault_type || "baseline workload"}${r.config?.latency_ms ? " " + r.config.latency_ms + "ms" : ""}${r.config?.duration_s ? " · planned " + r.config.duration_s + "s" : ""}`)],
    ["Fault window", r.fault_start ? `${fmtTime(r.fault_start)} → ${fmtTime(r.fault_end)}` : "–", r.fault_start && r.fault_end ? `${(r.fault_end - r.fault_start).toFixed(1)}s actual${o.abort_reason ? " · " + esc(o.abort_reason) : ""}` : esc(o.abort_reason || "")],
    ["Recovery", rec.verified == null ? "–" : rec.verified ? badge("verified", "st-good") : badge("NOT verified", "st-critical"), rec.recovery_time_s != null ? `recovered ${rec.recovery_time_s.toFixed(1)}s after fault removal` : esc(rec.notes || "")],
  ];
  $("#obs-cards").innerHTML = cards.map(([k, v, s]) => `<div class="mini"><div class="k">${k}</div><div class="v">${v}</div><div class="small muted">${s}</div></div>`).join("");
  legend($("#obs-legend"));
  const opts = { faultStart: r.fault_start, faultEnd: r.fault_end, recoveredAt };
  latencyChart($("#obs-latency"), r.samples, opts);
  errorChart($("#obs-errors"), r.samples, { ...opts, threshold: r.config?.thresholds?.max_error_rate });
  renderHealth($("#obs-health"), r.health, r);
  const prow = [];
  for (const [phase, ph] of Object.entries(metrics || {})) {
    const c = ph.critical;
    prow.push(`<tr style="font-weight:600"><td>${esc(phase)}</td><td>all critical</td><td class="num">${c.n}</td><td class="num">${c.errors}</td><td class="num">${fmtPct(c.error_rate)}</td><td class="num">${fmtMs(c.p50_ms)}</td><td class="num">${fmtMs(c.p95_ms)}</td></tr>`);
    for (const [p, s] of Object.entries(ph.probes)) prow.push(`<tr><td></td><td>${esc(p)}</td><td class="num">${s.n}</td><td class="num">${s.errors}</td><td class="num">${fmtPct(s.error_rate)}</td><td class="num">${fmtMs(s.p50_ms)}</td><td class="num">${fmtMs(s.p95_ms)}</td></tr>`);
  }
  rows($("#obs-phases tbody"), prow, 7, metrics ? "no samples" : "summary not available yet (run in progress or interrupted)");
  const imp = o.impact || {};
  $("#obs-impact").innerHTML = (imp.observations || []).map((x) => `<li>${esc(x)}</li>`).join("") + (imp.impact_detected != null ? `<li><b>Application impact detected: ${imp.impact_detected ? "yes" : "no"}</b></li>` : "") + (o.hypothesis ? `<li class="muted">Hypothesis: ${esc(o.hypothesis)}</li>` : "");
  $("#obs-recovery").innerHTML = r.kind !== "experiment" ? `<div class="muted">Baseline run: no fault injected.</div>` : `
    <table class="tbl"><tbody>
    <tr><td>Fault injected</td><td>${okBadge(o.injected ?? (r.fault_start ? true : null), "yes", "no")}</td><td class="small">${fmtTime(r.fault_start)}</td></tr>
    <tr><td>Fault removal verified</td><td>${okBadge(rem.verified)}</td><td class="small">${esc((rem.notes || []).join(" | "))}</td></tr>
    <tr><td>Recovery verified</td><td>${okBadge(rec.verified)}</td><td class="small">${esc(rec.notes || "")}</td></tr>
    <tr><td>Recovery time</td><td colspan="2">${rec.recovery_time_s != null ? rec.recovery_time_s.toFixed(1) + "s" : "–"}</td></tr>
    <tr><td>Threshold breaches</td><td colspan="2" class="small">${(o.breaches || []).map((b) => esc(b.message)).join("<br>") || "none"}</td></tr>
    </tbody></table>`;
  const failed = o.failed_checks || r.samples.filter((s) => !s.ok).slice(-60);
  rows($("#obs-failed tbody"), failed.slice().reverse().map((s) => `<tr><td>${fmtTime(s.ts)}</td><td>${esc(s.phase)}</td><td>${esc(s.probe)}</td><td class="num">${fmtMs(s.latency_ms)}</td><td class="small">${esc(s.detail)}</td></tr>`), 5, "no failed checks");
  const logs = o.logs || {};
  $("#obs-logs").innerHTML = Object.keys(logs).length ? Object.entries(logs).map(([svc, l]) => `<h3>${esc(svc)} <span class="muted small">${l.relevant.length} relevant of ${l.total} lines since fault start</span></h3><pre class="logs">${esc((l.relevant.length ? l.relevant : l.tail).join("\n")) || "(no log lines)"}</pre>`).join("") : `<div class="muted">${r.kind === "experiment" ? (TERMINAL.includes(r.state) ? "No logs captured (no fault was injected)." : "Logs are captured when the run finishes.") : "Baseline runs do not capture logs."}</div>`;
  rows($("#obs-events tbody"), (r.events || []).map((e) => `<tr><td>${fmtTime(e.ts)}</td><td>${badge(e.kind, e.level === "error" ? "st-critical" : e.level === "warning" ? "st-warning" : "st-good")}</td><td class="small">${esc(e.message)}</td></tr>`), 3, "no events");
}
function renderHealth(el, health, r) {
  if (!health.length) { el.innerHTML = `<div class="empty">No health samples.</div>`; return; }
  const by = {}; health.forEach((h) => (by[h.service] ||= []).push(h));
  const colorOf = (h) => { const c = statusClass(h.state === "running" ? h.health : h.state); return { "st-good": "var(--good)", "st-warning": "var(--warning)", "st-serious": "var(--serious)", "st-critical": "var(--critical)" }[c] || "var(--muted)"; };
  el.innerHTML = Object.entries(by).map(([svc, hs]) => {
    const counts = {}; hs.forEach((h) => { const k = h.state === "running" ? h.health : h.state; counts[k] = (counts[k] || 0) + 1; });
    return `<div class="small"><b>${esc(svc)}</b> <span class="muted">${Object.entries(counts).map(([k, v]) => `${k} ×${v}`).join(", ")}</span></div><div class="hstrip">${hs.map((h) => `<span style="background:${colorOf(h)}" title="${fmtTime(h.ts)} ${esc(h.phase)}: ${esc(h.state)}/${esc(h.health)}"></span>`).join("")}</div>`;
  }).join("") + `<div class="legend small">${[["healthy", "var(--good)"], ["starting", "var(--warning)"], ["unhealthy", "var(--serious)"], ["paused / down", "var(--critical)"]].map(([k, c]) => `<span><span class="sw" style="background:${c};height:10px"></span>${k}</span>`).join("")}</div>`;
}

// ---------------------------------------------------------------- polling loop
async function refreshAll() {
  await loadOverview();
  await pollLive();
}
(async function init() {
  await loadCatalog();
  await refreshAll();
  loadMonitorChart();
  setInterval(() => { if (currentTab === "overview") loadOverview(); else api("/api/safety").then(renderSafety).catch(() => {}); }, 3000);
  setInterval(pollLive, 1000);
  setInterval(() => { if (currentTab === "overview") loadMonitorChart(); }, 10000);
  setInterval(() => { if (currentTab === "graph") loadGraph(); }, 5000);
  setInterval(() => { if (currentTab === "obs") loadRuns(false); }, 6000);
})();
