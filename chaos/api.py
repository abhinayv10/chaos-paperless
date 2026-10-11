"""HTTP API + static dashboard. The API only *requests* actions from the
engine; safety enforcement lives in engine/safety and keeps working if no
browser is connected."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import catalog as experiment_catalog
from . import config, detection, llm, pipeline, runbooks
from .discovery import discover, load_overrides, save_overrides
from .docker_ops import Docker
from .engine import Engine, ExperimentRejected
from .evidence import build_evidence
from .grading import grade_incident
from .campaign import CampaignBusy, CampaignRunner
from .healer import GRAPH_NODES, Healer
from .models import FAULT_LABELS, ExperimentConfig
from .monitor import MONITOR_RUN, Monitor
from .orchestrator import Orchestrator
from .paperless_client import PaperlessClient
from .planner import make_plan
from .report import format_analysis, format_report
from .storage import Store
from .workload import INTERVALS, WORKLOAD_VERSION


class EdgeOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(..., pattern="^(add_edge|remove_edge|note)$")
    from_: Optional[str] = Field(None, alias="from", pattern=r"^[a-z0-9_-]{1,40}$")
    to: Optional[str] = Field(None, pattern=r"^[a-z0-9_-]{1,40}$")
    node: Optional[str] = Field(None, pattern=r"^[a-z0-9_-]{1,40}$")
    note: str = Field("", max_length=200)


class BaselineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duration_s: int = 30


class PlanRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ranks: Optional[list[int]] = Field(None, max_length=20)


class AgentSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class CampaignConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: list[str] = Field(..., max_length=10)


AUTO = "auto"
AGENT_KEY = "agent_mode"
# the agent needs time to learn, detect, ask the model and verify; shorter
# faults would end at the engine's deadline before the agent could act
AGENT_MIN_DURATION_S = 90


def create_app(store: Store | None = None, docker: Docker | None = None, engine: Engine | None = None,
               start_monitor: bool = True, provider=AUTO, settle_s: float | None = None,
               healer_kwargs: dict | None = None, agent_min_duration_s: int = AGENT_MIN_DURATION_S,
               agent_default: bool = False, scan_rounds: int = 5) -> FastAPI:
    store = store or Store()
    docker = docker or Docker()
    engine = engine or Engine(store, docker, PaperlessClient)
    monitor = Monitor(store, docker, PaperlessClient)

    def get_provider():
        """The configured LLM provider, or None (-> deterministic rules)."""
        if provider != AUTO:
            return provider
        try:
            return llm.get_provider()
        except llm.LLMError as e:
            store.event("ai_config_error", str(e), level="warning")
            return None

    healer = Healer(store, docker, get_provider, notify=engine.agent_remediated, **(healer_kwargs or {}))
    engine.on_start = healer.watch

    def agent_enabled() -> bool:
        v = store.kv_get(AGENT_KEY)
        return agent_default if v is None else bool(v.get("enabled"))

    def prepare(cfg: ExperimentConfig, explicit: bool = False) -> ExperimentConfig:
        """Self-healing mode on: hold the fault for the agent, long enough to act."""
        if explicit or not agent_enabled():
            return cfg
        return cfg.model_copy(update={"remediation": "agent",
                                      "duration_s": max(cfg.duration_s, agent_min_duration_s)})

    def analyze_run(run_id: str) -> dict:
        healer.wait(timeout=45)   # let the agent close its incident before the post-run analysis
        return pipeline.analyze_and_export(store, run_id, discover(docker), get_provider())

    orchestrator = Orchestrator(engine, store, after_run=analyze_run, prepare=prepare,
                                **({} if settle_s is None else {"settle_s": settle_s}))

    def campaign_wait(run_id: str) -> None:
        engine.wait()
        try:
            analyze_run(run_id)   # also waits for the self-healing agent to close its incident
        except Exception as e:  # noqa: BLE001 - analysis failure never affects the campaign's safety
            store.event("analysis_error", f"{type(e).__name__}: {e}", run_id, "warning")

    campaigns = CampaignRunner(store, docker, engine.client_factory, get_provider, engine.start, campaign_wait,
                               busy_fn=lambda: engine.busy() or orchestrator.running(),
                               duration_s=agent_min_duration_s, scan_rounds=scan_rounds)
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        report = engine.recover_on_startup()
        store.event("controller_started", f"startup recovery: {report}")
        if start_monitor:
            monitor.start()
        try:
            yield
        finally:
            monitor.stop()
            healer.stop()
            orchestrator.stop("controller shutting down")
            if engine.running_here():
                engine.kill("controller shutting down")

    app = FastAPI(title="Paperless chaos controller", version="day2", lifespan=lifespan)
    app.state.engine, app.state.store, app.state.monitor = engine, store, monitor
    app.state.orchestrator, app.state.healer, app.state.campaigns = orchestrator, healer, campaigns

    # ---------------------------------------------------------------- overview
    @app.get("/api/overview")
    def overview():
        baseline = store.latest_run("baseline", "completed")
        return {
            "now": time.time(),
            "monitor": monitor.latest,
            "baseline": baseline,
            "experiment": current(),
            "safety": safety(),
            "limits": catalog()["limits"],
        }

    @app.get("/api/metrics")
    def metrics():
        """The website's basic metrics over the last 15 minutes, with 12-point trends."""
        now, span, buckets = time.time(), 900.0, 12
        width = span / buckets
        rows = store.samples_since(now - span)
        reqs = [r for r in rows if r["probe"] != "doc_processing"]
        # documents are only uploaded during experiments: look further back for processing times
        procs = [r for r in store.samples_since(now - 6 * 3600) if r["probe"] == "doc_processing" and r["ok"]]

        def median(vals):
            vals = sorted(vals)
            return vals[len(vals) // 2] if vals else None

        def p95(vals):
            vals = sorted(vals)
            return vals[min(len(vals) - 1, int(0.95 * len(vals)))] if vals else None

        trend = {"rpm": [], "error_rate": [], "median_ms": [], "processing_ms": []}
        for i in range(buckets):
            lo = now - span + i * width
            b = [r for r in reqs if lo <= r["ts"] < lo + width]
            trend["rpm"].append(round(len(b) / (width / 60), 1))
            trend["error_rate"].append(round(sum(1 for r in b if not r["ok"]) / len(b), 3) if b else 0)
            trend["median_ms"].append(median([r["latency_ms"] for r in b]) or 0)
        recent = procs[-buckets * 3:]
        for i in range(0, len(recent), 3):
            trend["processing_ms"].append(median([r["latency_ms"] for r in recent[i:i + 3]]) or 0)
        hist = list(monitor.history)
        docs = [h["documents"] for h in hist if h["documents"] is not None]
        queue = [h["queue"] for h in hist if h["queue"] is not None]
        step = max(1, len(hist) // buckets)
        lat = [r["latency_ms"] for r in reqs]
        return {
            "now": now, "window_s": span,
            "requests": {"per_min": round(len(reqs) / (span / 60), 1), "total": len(reqs),
                         "failed": sum(1 for r in reqs if not r["ok"]),
                         "error_rate": (sum(1 for r in reqs if not r["ok"]) / len(reqs)) if reqs else None,
                         "median_ms": median(lat), "p95_ms": p95(lat)},
            "processing": {"median_ms": median([r["latency_ms"] for r in procs[-30:]]), "count": len(procs[-30:]),
                           "last_ts": procs[-1]["ts"] if procs else None},
            "documents": {"count": docs[-1] if docs else None,
                          "change": (docs[-1] - docs[0]) if len(docs) > 1 else None,
                          "trend": docs[::step][-buckets:]},
            "queue": {"length": queue[-1] if queue else None, "trend": queue[::step][-buckets:]},
            "trend": trend,
            "services": monitor.latest.get("services", []),
            "resources": monitor.resources,
            "disk": monitor.disk,
            "client_timeout_s": 5.0,
        }

    @app.get("/api/monitor")
    def monitor_series(minutes: int = 15):
        since = time.time() - max(1, min(minutes, 360)) * 60
        return {"samples": store.samples(MONITOR_RUN, since), "health": store.health(MONITOR_RUN, since)}

    # ---------------------------------------------------------------- graph
    @app.get("/api/graph")
    def graph():
        return discover(docker)

    @app.post("/api/graph/overrides")
    def add_override(o: EdgeOverride):
        data = load_overrides()
        if o.action in ("add_edge", "remove_edge"):
            if not (o.from_ and o.to) or o.from_ == o.to:
                raise HTTPException(400, "from and to are required and must differ")
            key = "add_edges" if o.action == "add_edge" else "remove_edges"
            item = {"from": o.from_, "to": o.to, "note": o.note, "ts": time.time()}
            data.setdefault(key, [])
            data[key] = [e for e in data[key] if (e["from"], e["to"]) != (o.from_, o.to)] + [item]
        else:
            if not o.node:
                raise HTTPException(400, "node is required")
            data.setdefault("notes", {})[o.node] = o.note
        save_overrides(data)
        store.event("graph_override", f"{o.action} {o.from_ or o.node}->{o.to or ''} {o.note}")
        return discover(docker)

    @app.delete("/api/graph/overrides")
    def remove_override(kind: str, src: str = "", dst: str = "", node: str = ""):
        data = load_overrides()
        if kind in ("add_edges", "remove_edges"):
            data[kind] = [e for e in data.get(kind, []) if (e["from"], e["to"]) != (src, dst)]
        elif kind == "notes":
            data.get("notes", {}).pop(node, None)
        else:
            raise HTTPException(400, "unknown override kind")
        save_overrides(data)
        return discover(docker)

    # ---------------------------------------------------------------- catalog
    @app.get("/api/catalog")
    def catalog():
        L = config.LIMITS
        return {
            "faults": FAULT_LABELS,
            "targets": {svc: sorted(f) for svc, f in L.allowlist.items()},
            "limits": {
                "min_duration_s": L.min_duration_s, "max_duration_s": L.max_duration_s,
                "min_latency_ms": L.min_latency_ms, "max_latency_ms": L.max_latency_ms,
                "threshold_latency_ms": [L.min_latency_threshold_ms, L.max_latency_threshold_ms],
                "threshold_window_s": [L.min_window_s, L.max_window_s],
                "watchdog_grace_s": L.watchdog_grace_s,
                "max_recovery_timeout_s": L.max_recovery_timeout_s,
            },
            "defaults": ExperimentConfig(target="db").model_dump(),
            "workload": {"version": WORKLOAD_VERSION, "intervals_s": INTERVALS},
        }

    # ---------------------------------------------------------------- experiments
    def _parse(payload: dict) -> ExperimentConfig:
        try:
            return ExperimentConfig.model_validate(payload)
        except ValidationError as e:
            raise HTTPException(422, [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()])

    @app.post("/api/experiments/preflight")
    def preflight(payload: dict = Body(...)):
        cfg = _parse(payload)
        errors, checks, _ = engine.preflight(cfg)
        return {"ok": not errors, "errors": errors, "checks": [c.to_dict() for c in checks]}

    @app.post("/api/experiments")
    def start(payload: dict = Body(...)):
        cfg = prepare(_parse(payload), explicit="remediation" in payload)
        try:
            run_id = engine.start(cfg)
        except ExperimentRejected as e:
            return JSONResponse(status_code=e.status, content={
                "ok": False, "errors": e.errors, "checks": [c.to_dict() for c in e.checks]})
        return {"ok": True, "run_id": run_id}

    @app.get("/api/experiments/current")
    def current():
        live = engine.live()
        holder = engine.safety.holder()
        if holder and holder.get("run_id") != live.get("run_id"):
            # run owned by another controller process (e.g. the CLI): show it from the store
            r = store.get_run(holder["run_id"]) or {}
            live = {"run_id": holder["run_id"], "kind": r.get("kind"), "state": r.get("state"),
                    "config": r.get("config"), "fault_start": r.get("fault_start"),
                    "fault_end": r.get("fault_end"), "message": f"running in process {holder.get('pid')}",
                    "phase": None, "breaches": []}
        if live.get("run_id"):
            live["samples"] = store.samples(live["run_id"])
            live["health"] = store.health(live["run_id"])
            live["events"] = store.events(live["run_id"], limit=50)
        return {"busy": engine.busy(), "holder": holder, "live": live}

    @app.post("/api/baseline")
    def baseline(req: BaselineRequest):
        try:
            return {"ok": True, "run_id": engine.start_baseline(req.duration_s)}
        except ExperimentRejected as e:
            return JSONResponse(status_code=e.status, content={"ok": False, "errors": e.errors})

    @app.post("/api/kill")
    def kill():
        orchestrator.stop("manual kill switch")  # no further planned experiments start
        campaigns._stop.set()                     # nor further campaign experiments
        healer.stop()
        return engine.kill("manual kill switch (dashboard/API)")

    # ---------------------------------------------------------------- self-healing agent
    @app.get("/api/agent")
    def agent_status():
        return {
            "enabled": agent_enabled(), "watching": healer.running(),
            "min_duration_s": agent_min_duration_s, "graph": GRAPH_NODES,
            "failure_classes": detection.FAILURE_CLASSES,
            "runbooks": [{"failure_class": k, **v} for k, v in runbooks.RUNBOOKS.items()],
            "detection": {"window_s": detection.WINDOW_S, "error_min": detection.ERR_MIN,
                          "error_rise": detection.ERR_RISE, "min_failures": detection.MIN_FAILURES,
                          "latency_ratio": detection.LAT_RATIO, "latency_min_delta_ms": detection.LAT_MIN_DELTA_MS,
                          "confirm_checks": detection.CONFIRM_TICKS},
            "probe_depends": detection.PROBE_DEPENDS,
            "ai": llm.describe(get_provider()),
        }

    @app.post("/api/agent")
    def set_agent(s: AgentSetting):
        store.kv_set(AGENT_KEY, {"enabled": s.enabled})
        store.event("agent_mode", f"self-healing mode {'enabled' if s.enabled else 'disabled'}")
        return agent_status()

    # ---------------------------------------------------------------- campaign (scan -> select -> confirm -> inject)
    @app.post("/api/campaign")
    def campaign_start():
        try:
            return {"ok": True, "id": campaigns.start()}
        except CampaignBusy as e:
            return JSONResponse(status_code=409, content={"ok": False, "errors": [str(e)]})

    @app.get("/api/campaign/latest")
    def campaign_latest():
        items = store.list_campaigns(1)
        return {"campaign": items[0] if items else None, "running": campaigns.running(),
                "awaiting": campaigns.awaiting()}

    @app.get("/api/campaign/{campaign_id}")
    def campaign_get(campaign_id: str):
        doc = store.get_campaign(campaign_id)
        if not doc:
            raise HTTPException(404, "campaign not found")
        return {"campaign": doc, "running": campaigns.running() and campaigns.current == campaign_id}

    @app.post("/api/campaign/{campaign_id}/confirm")
    def campaign_confirm(campaign_id: str, req: CampaignConfirm):
        try:
            campaigns.confirm(campaign_id, req.approved)
        except CampaignBusy as e:
            return JSONResponse(status_code=409, content={"ok": False, "errors": [str(e)]})
        except ValueError as e:
            return JSONResponse(status_code=400, content={"ok": False, "errors": [str(e)]})
        store.event("campaign_confirmed", f"{campaign_id}: approved {', '.join(req.approved)}")
        return {"ok": True}

    @app.post("/api/campaign/{campaign_id}/cancel")
    def campaign_cancel(campaign_id: str):
        try:
            campaigns.cancel(campaign_id)
        except ValueError as e:
            return JSONResponse(status_code=400, content={"ok": False, "errors": [str(e)]})
        return {"ok": True}

    @app.get("/api/incidents")
    def incidents(limit: int = 30):
        out = []
        for inc in store.list_incidents(min(limit, 100)):
            atts = inc.get("attempts") or []
            out.append({k: inc.get(k) for k in ("id", "run_id", "created", "status", "detected_at",
                                                "resolved_at", "ended")}
                       | {"attempts": len(atts), "mode": atts[-1]["source"]["mode"] if atts else None,
                          "grade": grade_incident(store, inc)})
        return out

    @app.get("/api/incidents/latest")
    def latest_incident():
        items = store.list_incidents(1)
        if not items:
            return {"incident": None}
        return {"incident": items[0], "grade": grade_incident(store, items[0])}

    @app.get("/api/incidents/{incident_id}")
    def incident(incident_id: str):
        inc = store.get_incident(incident_id)
        if not inc:
            raise HTTPException(404, "incident not found")
        return {"incident": inc, "grade": grade_incident(store, inc)}

    # ---------------------------------------------------------------- AI planner (agent 1)
    @app.get("/api/ai/status")
    def ai_status():
        return llm.describe(get_provider())

    @app.post("/api/plan")
    def create_plan():
        return make_plan(store, discover(docker), get_provider())

    @app.get("/api/plan")
    def latest_plan():
        return {"plan": store.latest_plan(), "sequence_running": orchestrator.running(),
                "capabilities": experiment_catalog.capabilities(),
                "workflows": experiment_catalog.WORKFLOWS}

    @app.post("/api/plan/{plan_id}/run")
    def run_plan(plan_id: str, req: PlanRunRequest):
        try:
            return {"ok": True} | orchestrator.start(plan_id, req.ranks)
        except ExperimentRejected as e:
            return JSONResponse(status_code=e.status, content={"ok": False, "errors": e.errors})

    # ---------------------------------------------------------------- analyst (agent 2) + scores
    @app.post("/api/runs/{run_id}/analyze")
    def analyze(run_id: str):
        try:
            return analyze_run(run_id)
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/runs/{run_id}/analysis")
    def analysis(run_id: str):
        a = store.get_analysis(run_id)
        if not a:
            raise HTTPException(404, "run has not been analysed")
        return a

    @app.get("/api/runs/{run_id}/export")
    def export(run_id: str):
        ev = build_evidence(store, run_id)
        if ev is None:
            raise HTTPException(404, "experiment not found")
        return {"evidence": ev, "analysis": store.get_analysis(run_id)}

    @app.post("/api/analyze-all")
    def analyze_all():
        """Analyse every finished experiment that has no finding yet."""
        done, errors = [], {}
        graph, prov = discover(docker), get_provider()
        for run_id in pipeline.analyzable_runs(store):
            if store.get_analysis(run_id):
                continue
            try:
                pipeline.analyze_and_export(store, run_id, graph, prov)
                done.append(run_id)
            except Exception as e:  # noqa: BLE001
                errors[run_id] = f"{type(e).__name__}: {e}"
        return {"analysed": done, "errors": errors}

    @app.get("/api/findings")
    def findings():
        return pipeline.findings_summary(store)

    @app.get("/api/report", response_class=PlainTextResponse)
    def findings_report():
        return pipeline.full_report(store, docker)

    # ---------------------------------------------------------------- runs
    @app.get("/api/runs")
    def runs(kind: Optional[str] = None, limit: int = 30):
        out = []
        for r in store.list_runs(kind, min(limit, 200)):
            o = r.get("outcome") or {}
            out.append({k: r.get(k) for k in ("id", "kind", "state", "created", "fault_start", "fault_end", "ended")}
                       | {"config": r.get("config"), "result": o.get("result"),
                          "abort_reason": o.get("abort_reason"),
                          "impact": (o.get("impact") or {}).get("impact_detected"),
                          "analysed": store.get_analysis(r["id"]) is not None,
                          "recovery_s": (o.get("recovery") or {}).get("recovery_time_s")})
        return out

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        r = store.get_run(run_id)
        if not r:
            raise HTTPException(404, "run not found")
        r["samples"] = store.samples(run_id)
        r["health"] = store.health(run_id)
        r["events"] = store.events(run_id, limit=300)
        return r

    @app.get("/api/runs/{run_id}/report", response_class=PlainTextResponse)
    def run_report(run_id: str):
        r = store.get_run(run_id)
        if not r or r["kind"] != "experiment":
            raise HTTPException(404, "experiment not found")
        a = store.get_analysis(run_id)
        return format_report(r) + ("\n" + format_analysis(a) if a else "")

    # ---------------------------------------------------------------- safety
    @app.get("/api/safety")
    def safety():
        return {
            "blocked": engine.safety.blocked(),
            "active_fault": engine.safety.active(),
            "kill_requested": engine.safety.kill_requested(),
            "events": store.events(limit=40, kinds=(
                "threshold_breach", "manual_abort", "kill_switch", "kill_rollback", "fault_injected",
                "fault_removed", "rollback_failed", "blocked", "unblocked", "unblock_refused",
                "watchdog_armed", "watchdog_rollback", "watchdog_rollback_done", "crash_recovery",
                "crash_recovery_done", "rejected", "recovery_verified", "recovery_failed")),
        }

    @app.post("/api/safety/unblock")
    def unblock():
        ok, checks = engine.unblock()
        return {"ok": ok, "checks": [c.to_dict() for c in checks]}

    @app.get("/api/environment")
    def environment():
        checks = engine.verify_environment()
        return {"ok": all(c.ok for c in checks), "checks": [c.to_dict() for c in checks]}

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # ---------------------------------------------------------------- dashboard
    app.mount("/static", StaticFiles(directory=config.DASHBOARD_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(config.DASHBOARD_DIR / "index.html")

    return app
