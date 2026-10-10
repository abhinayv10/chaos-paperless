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
from . import config, llm, pipeline
from .discovery import discover, load_overrides, save_overrides
from .docker_ops import Docker
from .engine import Engine, ExperimentRejected
from .evidence import build_evidence
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


AUTO = "auto"


def create_app(store: Store | None = None, docker: Docker | None = None, engine: Engine | None = None,
               start_monitor: bool = True, provider=AUTO, settle_s: float | None = None) -> FastAPI:
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

    def analyze_run(run_id: str) -> dict:
        return pipeline.analyze_and_export(store, run_id, discover(docker), get_provider())

    orchestrator = Orchestrator(engine, store, after_run=analyze_run,
                                **({} if settle_s is None else {"settle_s": settle_s}))
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
            orchestrator.stop("controller shutting down")
            if engine.running_here():
                engine.kill("controller shutting down")

    app = FastAPI(title="Paperless chaos controller", version="day2", lifespan=lifespan)
    app.state.engine, app.state.store, app.state.monitor = engine, store, monitor
    app.state.orchestrator = orchestrator

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
        cfg = _parse(payload)
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
        return engine.kill("manual kill switch (dashboard/API)")

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
