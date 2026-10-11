"""Self-healing agent: a LangGraph state machine that detects, diagnoses and
fixes a dependency failure while an experiment is running.

    observe -> detect --(anomaly confirmed)--> diagnose -> select_runbook -> remediate -> verify
       ^         |                                ^              |                          |
       +---------+ (healthy)                      +-- retry -----)-- (not fixed, attempts left)
                                                                 +--> finalize <------------+ (fixed / escalated)

Who decides what:
  detect          deterministic statistics (detection.py), never the AI
  diagnose        the AI names ONE failure class and must cite the signal ids
                  behind every reasoning step; rules take over if it fails
  select_runbook  a fixed table (runbooks.py), never the AI
  remediate       the runbook's fixed docker action on an allowlisted service
  verify          the target condition is gone AND the application answers again

The agent is blind to the experiment: it is given a run id and reads only
probe samples (by timestamp) and container states from storage. It never
reads the run's config, the fault spec or the phase labels. Whether its
diagnosis was right is graded afterwards by the API, outside this module.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Literal, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import detection, runbooks
from .detection import FAILURE_CLASSES, PROBE_DEPENDS
from .docker_ops import Docker
from .llm import LLMError, Provider, describe
from .storage import Store

TICK_S = 1.5
LEARN_S = 12.0
VERIFY_TIMEOUT_S = 60.0
EVIDENCE_WAIT_S = 6.0   # just over the 5 s probe timeout: long enough for a hung request to register
MAX_ATTEMPTS = 2
RECURSION_LIMIT = 5000
KILL_KEY = "kill_requested"     # SafetyState.KILL: the kill switch also stops the agent
TERMINAL = {"completed", "aborted", "failed", "rejected", "interrupted"}

FailureClass = Literal["db_unavailable", "db_slow", "db_cpu_starved", "broker_unavailable", "broker_slow",
                       "broker_cpu_starved", "db_refusing_connections", "broker_refusing_connections",
                       "db_memory_exhausted", "broker_memory_exhausted", "webserver_disk_full", "unknown"]


# ---------------------------------------------------------------- AI diagnosis contract
class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: str = Field(..., min_length=1, max_length=500)
    cites: list[str] = Field(default_factory=list, max_length=12)


class RuledOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    failure_class: FailureClass
    why: str = Field(..., min_length=1, max_length=400)
    cites: list[str] = Field(default_factory=list, max_length=12)


class AgentDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    failure_class: FailureClass
    confidence: Literal["low", "medium", "high"]
    summary: str = Field(..., min_length=1, max_length=600)
    reasoning: list[Step] = Field(..., min_length=1, max_length=8)
    ruled_out: list[RuledOut] = Field(default_factory=list, max_length=4)


class InvalidDiagnosis(LLMError):
    """The model answered, but the answer broke the contract (worth one retry)."""


def validate_diagnosis(raw: object, snapshot: dict, excluded: list[str]) -> dict:
    try:
        d = AgentDiagnosis.model_validate(raw).model_dump()
    except ValidationError as e:
        raise InvalidDiagnosis("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                                         for x in e.errors()[:8])) from e
    ids = detection.signal_ids(snapshot)
    cited = [c for s in d["reasoning"] for c in s["cites"]] + [c for r in d["ruled_out"] for c in r["cites"]]
    errors = []
    unknown = sorted({c for c in cited if c not in ids})
    if unknown:
        errors.append(f"cited signal ids do not exist: {unknown} (valid: {sorted(ids)})")
    if d["failure_class"] != "unknown" and not any(c.startswith("A") for s in d["reasoning"] for c in s["cites"]):
        errors.append("a diagnosis must cite at least one anomaly (A...) in its reasoning")
    if any(not s["cites"] for s in d["reasoning"]):
        errors.append("every reasoning step must cite at least one signal id")
    if d["failure_class"] in excluded:
        errors.append(f"'{d['failure_class']}' is excluded: its runbook already ran and the anomalies persisted")
    if errors:
        raise InvalidDiagnosis("; ".join(errors))
    return d


SYSTEM = """You are the diagnosis step of a self-healing agent for a Paperless-ngx deployment \
(webserver, PostgreSQL database "db", Valkey/Redis message broker "broker"). Something is wrong right now.

You get numbered signals computed from live measurements against a learned healthy baseline:
anomalies (A1, A2, ...) and signals that are still normal (N1, N2, ...). Decide which ONE failure class \
explains them.

Rules:
- The signals are your only evidence. Cite signal ids in every reasoning step. Never invent measurements.
- Use workflow_dependencies: a workflow can only be broken by a service it depends on, and a workflow that \
is still normal is evidence against the services it depends on.
- "<service>_unavailable" means the dependency is not answering at all (its container is paused, frozen or \
stopped). "<service>_cpu_starved" means its container is running but pinned at a CPU quota. "<service>_slow" \
means its container is running normally (not CPU-bound) but requests that need it are delayed, lose packets or \
time out: the network path is impaired. "<service>_refusing_connections" means its container is running but \
requests that need it fail immediately (rejected), not after a long wait. "<service>_memory_exhausted" means it is \
squeezed by a memory limit (OOM kills, restarts). "webserver_disk_full" means the web application's upload \
scratch disk is full (uploads fail, reads work). A signal saying failures are "fast" or "slow" is decisive between \
refusing and slow/unavailable.
- You do not choose or perform the fix. A fixed rule table maps your class to a runbook. If the signals do not \
support one class, answer "unknown": that escalates to a human and changes nothing.
- Never answer a class listed in excluded_classes: its runbook already ran and the anomalies persisted.
- List the strongest alternatives you ruled out, with the signal ids that rule them out.

Reply with one JSON object and nothing else:
{"failure_class": "one of the failure_classes keys",
 "confidence": "low|medium|high", "summary": "one sentence",
 "reasoning": [{"step": "...", "cites": ["A1", "N2"]}],
 "ruled_out": [{"failure_class": "...", "why": "...", "cites": ["N1"]}]}"""


def build_prompt(snapshot: dict, attempts: list[dict], excluded: list[str]) -> str:
    keep = ("id", "signal", "metric", "baseline", "observed", "deviation", "rule")
    payload = {
        "workflow_dependencies": PROBE_DEPENDS,
        "failure_classes": FAILURE_CLASSES,
        "anomalies": [{k: a[k] for k in keep if k in a} for a in snapshot["anomalies"]],
        "normals": [{"id": n["id"], "signal": n["signal"]} for n in snapshot["normals"]],
        "previous_attempts": [{"failure_class": a["diagnosis"]["failure_class"], "runbook": a["runbook"]["title"],
                               "result": "the runbook ran and the anomalies persisted"}
                              for a in attempts if a.get("verification") and not a["verification"]["ok"]],
        "excluded_classes": excluded,
    }
    return "Signals (JSON):\n" + json.dumps(payload, indent=1)


def _workflow_evidence(snap: dict) -> bool:
    return any(a["kind"] in ("error_rate", "latency", "dependency_report") for a in snap["anomalies"])


# ---------------------------------------------------------------- graph state
class HealState(TypedDict, total=False):
    incident: dict
    route: str


class IncidentAgent:
    """One agent instance per watched run. Nodes are methods; the compiled
    LangGraph drives them."""

    def __init__(self, store: Store, docker: Docker, run_id: str,
                 provider_fn: Callable[[], Optional[Provider]],
                 notify: Optional[Callable[[str, str], dict]] = None,
                 stop: Optional[threading.Event] = None, tick_s: float = TICK_S, learn_s: float = LEARN_S,
                 verify_timeout_s: float = VERIFY_TIMEOUT_S, window_s: float = detection.WINDOW_S,
                 evidence_wait_s: float = EVIDENCE_WAIT_S):
        self.store, self.docker, self.run_id = store, docker, run_id
        self.provider_fn, self.notify = provider_fn, notify
        self.stop = stop or threading.Event()
        self.tick_s, self.learn_s, self.verify_timeout_s, self.window_s = tick_s, learn_s, verify_timeout_s, window_s
        self.evidence_wait_s = evidence_wait_s
        self.streak = 0
        self.first_sample_ts: Optional[float] = None
        self.reference: Optional[dict] = None
        self.excluded: list[str] = []

    # ---- helpers
    def _trace(self, inc: dict, node: str, text: str) -> None:
        inc["trace"].append({"node": node, "ts": time.time(), "text": text})
        inc["node"] = node
        self.store.save_incident(inc)

    def _run_over(self) -> Optional[str]:
        """Only the run's lifecycle state is read here, never its config."""
        if self.stop.is_set():
            return "agent stopped"
        if self.store.kv_get(KILL_KEY):
            return "kill switch"
        state = self.store.run_state(self.run_id)
        if state in TERMINAL:
            return f"experiment ended ({state})"
        return None

    def _snapshot(self, live: bool = False) -> dict:
        """Signals over the last window. live=True reads container states from
        Docker now instead of the last stored sample (which can be seconds old)."""
        now = time.time()
        since = now - self.window_s - 1
        health = self.store.health(self.run_id, since=since)
        resources = None
        if live:
            try:
                cs = [c for c in self.docker.project_containers() if c.service]
                health = [{"service": c.service, "state": "paused" if c.paused else c.state,
                           "health": c.health, "ts": now} for c in cs]
                usage = self.docker.stats([c.id for c in cs if c.running and not c.paused])
                resources = {c.service: {"cpu_pct": u["cpu_pct"], "cpu_limit": c.cpu_limit,
                                         "mem_pct": u.get("mem_pct", 0.0), "memory_mb": c.memory / 2 ** 20,
                                         "oom_killed": c.oom_killed, "restart_count": c.restart_count}
                             for c in cs for cid, u in usage.items()
                             if cid and (c.id.startswith(cid) or cid.startswith(c.id))}
                for c in cs:   # a restarting (OOM-looping) container has no stats but its limit still counts
                    if c.service not in resources and c.memory:
                        resources[c.service] = {"memory_mb": c.memory / 2 ** 20, "mem_pct": 100.0,
                                                "oom_killed": c.oom_killed, "restart_count": c.restart_count}
                try:
                    use = self.docker.disk_usage("agent")
                    resources.setdefault("webserver", {}).update(scratch_pct=use["used_pct"],
                                                                 scratch_mb=use["total_mb"])
                except Exception:  # noqa: BLE001 - no scratch volume: no disk signal
                    pass
            except Exception:  # noqa: BLE001 - fall back to the stored samples
                pass
        return detection.detect(self.store.samples(self.run_id, since=since), health,
                                self.reference or {}, now, self.window_s, resources=resources)

    # ---- nodes
    def observe(self, state: HealState) -> HealState:
        inc = state["incident"]
        if inc["trace"]:
            self.stop.wait(self.tick_s)
        over = self._run_over()
        if over:
            inc["end_reason"] = over
            return {"incident": inc, "route": "finalize"}
        if self.reference is None:
            samples = self.store.samples(self.run_id)
            if samples and self.first_sample_ts is None:
                self.first_sample_ts = samples[0]["ts"]
            if self.first_sample_ts and time.time() - self.first_sample_ts >= self.learn_s:
                learn = [s for s in samples if s["ts"] < self.first_sample_ts + self.learn_s]
                self.reference = detection.learn_reference(learn)
                inc["reference"] = self.reference
                inc["status"] = "watching"
                self._trace(inc, "observe", f"learned the healthy baseline from {len(learn)} requests over "
                                            f"{self.learn_s:.0f}s; now watching")
            elif not inc["trace"]:
                self._trace(inc, "observe", "learning the healthy baseline")
            return {"incident": inc, "route": "observe"}
        return {"incident": inc, "route": "detect"}

    def detect(self, state: HealState) -> HealState:
        inc = state["incident"]
        snap = self._snapshot()
        inc["live"] = {"ts": snap["ts"], "anomalies": len(snap["anomalies"]),
                       "signals": [a["signal"] for a in snap["anomalies"]][:6]}
        self.streak = self.streak + 1 if snap["anomalies"] else 0
        if self.streak >= detection.CONFIRM_TICKS:
            inc.update(status="detected", detected_at=snap["ts"], detection=snap)
            self._trace(inc, "detect", f"failure detected: {len(snap['anomalies'])} anomalies confirmed on "
                                       f"{self.streak} consecutive checks")
            return {"incident": inc, "route": "diagnose"}
        self.store.save_incident(inc)
        return {"incident": inc, "route": "observe"}

    def diagnose(self, state: HealState) -> HealState:
        inc = state["incident"]
        n = len(inc["attempts"]) + 1
        inc["status"] = "diagnosing"
        snap = self._snapshot(live=True)
        if n == 1 and not _workflow_evidence(snap):
            # A container-state signal can fire before any request has timed out.
            # Give the workflows a few seconds to show their side, so the
            # diagnosis reasons from user-facing metrics, not one container flag.
            self._trace(inc, "diagnose", f"gathering workflow evidence (up to {self.evidence_wait_s:.0f}s)")
            deadline = time.time() + self.evidence_wait_s
            while time.time() < deadline and not self.stop.is_set():
                self.stop.wait(self.tick_s)
                snap = self._snapshot(live=True)
                if _workflow_evidence(snap):
                    break
        self._trace(inc, "diagnose", f"attempt {n}: classifying {len(snap['anomalies'])} anomalies "
                                     f"and {len(snap['normals'])} normal signals")
        provider = self.provider_fn()
        source = describe(provider) | {"mode": "rules", "attempts": 0, "error": None}
        diag = None
        if provider is not None:
            user, feedback = build_prompt(snap, inc["attempts"], self.excluded), ""
            for k in (1, 2):
                source["attempts"] = k
                try:
                    diag = validate_diagnosis(provider.complete_json(SYSTEM, user + feedback), snap, self.excluded)
                    source.update(mode="llm", error=None, model=getattr(provider, "last_model", source.get("model")))
                    break
                except InvalidDiagnosis as e:
                    source["error"] = str(e)
                    feedback = f"\n\nYour previous reply was rejected: {e}\nReturn a corrected JSON object."
                except LLMError as e:   # provider down / rate limited: no point retrying now
                    source["error"] = str(e)
                    break
                except Exception as e:  # noqa: BLE001
                    source["error"] = f"{type(e).__name__}: {e}"
                    break
            if diag is None:
                source["mode"] = "rules-fallback"
        rules = detection.rules_diagnosis(snap, self.excluded)
        if diag is None:
            diag = rules
        attempt = {"n": n, "started": time.time(), "snapshot": snap, "diagnosis": diag, "source": source,
                   "rules_check": {"failure_class": rules["failure_class"],
                                   "agrees": rules["failure_class"] == diag["failure_class"]}}
        inc["attempts"].append(attempt)
        who = f"AI ({source['model']})" if source["mode"] == "llm" else "rules"
        self._trace(inc, "diagnose", f"{who}: {diag['failure_class']} ({diag['confidence']} confidence)")
        return {"incident": inc, "route": "select_runbook"}

    def select_runbook(self, state: HealState) -> HealState:
        inc = state["incident"]
        att = inc["attempts"][-1]
        rb = runbooks.runbook_for(att["diagnosis"]["failure_class"])
        att["runbook"] = rb
        self._trace(inc, "select_runbook", f"rule: {rb['failure_class']} -> {rb['id']} {rb['title']}")
        if not rb["action"]:
            inc["status"] = "escalated"
            att["execution"] = runbooks.execute(self.docker, rb)
            inc["end_reason"] = "diagnosis could not attribute the failure; escalated to a human"
            return {"incident": inc, "route": "finalize"}
        return {"incident": inc, "route": "remediate"}

    def remediate(self, state: HealState) -> HealState:
        inc = state["incident"]
        att = inc["attempts"][-1]
        inc["status"] = "remediating"
        self._trace(inc, "remediate", f"running {att['runbook']['id']}: {att['runbook']['title']}")
        att["execution"] = runbooks.execute(self.docker, att["runbook"])
        att["remediated_at"] = time.time()
        if self.notify:
            try:
                att["engine_ack"] = self.notify(self.run_id, f"{att['runbook']['id']} {att['runbook']['title']}")
            except Exception as e:  # noqa: BLE001
                att["engine_ack"] = {"error": f"{type(e).__name__}: {e}"}
        ok = att["execution"]["ok"]
        self._trace(inc, "remediate", ("runbook completed" if ok else "runbook step failed") + ": "
                    + "; ".join(f"{s['step']}: {s['detail']}" for s in att["execution"]["steps"])[:300])
        return {"incident": inc, "route": "verify"}

    def _app_ok(self, since: float) -> tuple[bool, str]:
        rows = [s for s in self.store.samples(self.run_id, since=since) if s["probe"] in detection.PROBES]
        latest = {}
        for s in rows:
            latest[s["probe"]] = s
        good = sum(1 for s in rows if s["ok"])
        need = [p for p in ("api_documents", "api_status") if p in latest]
        ok = len(need) == 2 and all(latest[p]["ok"] for p in need) and good >= 3 \
            and all(s["ok"] for s in latest.values())
        return ok, (f"{good}/{len(rows)} requests ok since the fix; latest: "
                    + ", ".join(f"{p}={'ok' if s['ok'] else 'FAILED'}" for p, s in sorted(latest.items())))

    def verify(self, state: HealState) -> HealState:
        inc = state["incident"]
        att = inc["attempts"][-1]
        inc["status"] = "verifying"
        self._trace(inc, "verify", "checking the target and waiting for the application to answer normally")
        t0 = att["remediated_at"]
        deadline = t0 + self.verify_timeout_s
        target_ok, tdetail, app_ok, adetail = False, "", False, "no requests yet"
        while time.time() < deadline and not self.stop.is_set():
            target_ok, tdetail = runbooks.target_check(self.docker, att["runbook"])
            app_ok, adetail = self._app_ok(t0)
            if target_ok and app_ok:
                break
            self.stop.wait(self.tick_s)
        ok = target_ok and app_ok
        att["verification"] = {"ok": ok, "at": time.time(), "checks": [
            {"check": att["runbook"]["checks"][0] if att["runbook"]["checks"] else "target", "ok": target_ok,
             "detail": tdetail},
            {"check": "application requests succeed again", "ok": app_ok, "detail": adetail}]}
        if ok:
            inc.update(status="resolved", resolved_at=att["verification"]["at"])
            self._trace(inc, "verify", f"fixed: {tdetail}; {adetail}")
            return {"incident": inc, "route": "finalize"}
        self.excluded.append(att["diagnosis"]["failure_class"])
        self._trace(inc, "verify", f"not fixed after {self.verify_timeout_s:.0f}s: {tdetail}; {adetail}")
        if len(inc["attempts"]) < MAX_ATTEMPTS and not self._run_over():
            return {"incident": inc, "route": "diagnose"}
        inc["status"] = "escalated"
        inc["end_reason"] = f"not fixed after {len(inc['attempts'])} runbook(s); escalated to a human"
        return {"incident": inc, "route": "finalize"}

    def finalize(self, state: HealState) -> HealState:
        inc = state["incident"]
        if inc["status"] in ("learning", "watching"):
            inc["status"] = "no_incident"
        elif inc["status"] not in ("resolved", "escalated"):
            inc["status"] = "interrupted"
        inc["ended"] = time.time()
        self._trace(inc, "finalize", f"{inc['status']}" + (f": {inc.get('end_reason')}" if inc.get("end_reason") else ""))
        return {"incident": inc, "route": "end"}

    # ---- graph
    def graph(self):
        g = StateGraph(HealState)
        for name in ("observe", "detect", "diagnose", "select_runbook", "remediate", "verify", "finalize"):
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "observe")
        route = lambda s: s["route"]  # noqa: E731
        g.add_conditional_edges("observe", route, {"observe": "observe", "detect": "detect", "finalize": "finalize"})
        g.add_conditional_edges("detect", route, {"observe": "observe", "diagnose": "diagnose"})
        g.add_edge("diagnose", "select_runbook")
        g.add_conditional_edges("select_runbook", route, {"remediate": "remediate", "finalize": "finalize"})
        g.add_edge("remediate", "verify")
        g.add_conditional_edges("verify", route, {"diagnose": "diagnose", "finalize": "finalize"})
        g.add_edge("finalize", END)
        return g.compile()

    def run(self) -> dict:
        inc = {"id": f"inc-{self.run_id}", "run_id": self.run_id, "created": time.time(), "status": "learning",
               "node": None, "trace": [], "attempts": [], "reference": None, "detection": None, "live": None,
               "detected_at": None, "resolved_at": None, "ended": None, "end_reason": None}
        self.store.save_incident(inc)
        try:
            out = self.graph().invoke({"incident": inc, "route": "observe"},
                                      config={"recursion_limit": RECURSION_LIMIT})
            return out["incident"]
        except Exception as e:  # noqa: BLE001 - the engine's own safety net still removes the fault
            inc.update(status="interrupted", ended=time.time(), end_reason=f"agent error: {type(e).__name__}: {e}")
            self.store.save_incident(inc)
            self.store.event("agent_error", inc["end_reason"], self.run_id, "error")
            return inc


GRAPH_NODES = ["observe", "detect", "diagnose", "select_runbook", "remediate", "verify", "finalize"]


class Healer:
    """Starts one IncidentAgent thread per watched experiment."""

    def __init__(self, store: Store, docker: Docker, provider_fn: Callable[[], Optional[Provider]],
                 notify: Optional[Callable[[str, str], dict]] = None, **agent_kwargs):
        self.store, self.docker, self.provider_fn, self.notify = store, docker, provider_fn, notify
        self.agent_kwargs = agent_kwargs
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def watch(self, run_id: str) -> str:
        """Called with nothing but the run id when an experiment starts."""
        self.stop()
        if self._thread:
            self._thread.join(timeout=5)
        self._stop = threading.Event()
        agent = IncidentAgent(self.store, self.docker, run_id, self.provider_fn, self.notify,
                              stop=self._stop, **self.agent_kwargs)
        self._thread = threading.Thread(target=agent.run, name=f"healer-{run_id}", daemon=True)
        self._thread.start()
        self.store.event("agent_watching", "self-healing agent attached (blind to the fault)", run_id)
        return f"inc-{run_id}"

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def stop(self) -> None:
        self._stop.set()
