# Paperless-ngx Chaos Lab

An AI-driven chaos-engineering prototype for the Bosch hackathon. It runs
[Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) (GPL-3.0) in an isolated Docker Compose
sandbox, finds its weak points, breaks it on purpose in bounded and reversible ways, and lets a self-healing agent
detect, explain and fix each failure, while the dashboard shows how the website reacts before, during and after.

The loop, driven from the dashboard's **Scan & inject** page:

1. **Scan.** A live, read-only resilience scan of the running application finds its weak points (single points of
   failure, no automatic recovery, no resource budgets, little latency headroom, no circuit breaker, a small upload
   disk), each with measured evidence.
2. **Rank and select (AI).** The AI ranks the weak points and proposes failure experiments that test every one of them.
3. **Confirm (you).** Nothing is injected until a human approves at least 3 experiments.
4. **Inject (AI agent + tool).** An execution agent injects each approved experiment through an `inject_fault` tool
   that refuses anything you did not approve. Every run goes through the safety-controlled engine.
5. **Self-heal.** A LangGraph agent that is not told what was injected detects the failure from metric anomalies,
   has the AI name the failure class (citing the measurements behind every reasoning step), applies the
   **predetermined runbook** a fixed rule table assigns to that class, and verifies the fix. The AI never chooses or
   performs the fix itself.
6. **Analyse and report.** The analyst diagnoses each run and recommends a fix; scores are computed, never taken from
   the AI; a findings table shows, for every weak point, whether it was confirmed, what happened and how self-healing did.

The method follows the [`testing-chaos`](https://www.skills.sh/wojons/skills/testing-chaos) skill: hypothesis-driven
experiments, a contained blast radius, baseline metrics before injection, small controlled faults, automatic
rollback plus a manual abort, verification afterwards, and a results report.

---

## 1. Prerequisites

| Need | Notes |
|---|---|
| Docker Desktop (WSL2 backend) or Docker Engine + Compose v2 | `docker version` must show a **Server** section; Linux containers |
| Python 3.11+ | tested with 3.12 and 3.13 |
| ~3 GB disk for images | the Paperless image is about 1.5 GB |
| An AI model (optional) | Gemini, Anthropic, Ollama or any OpenAI-compatible endpoint; without one, deterministic rules take over |

On Windows: install WSL2 (`wsl --install`, then reboot), then install and start Docker Desktop.

## 2. Setup and startup

```powershell
cd chaos-paperless
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\python -m chaos up        # generates sandbox/.env secrets, builds the helper image, starts the stack, waits until healthy
.venv\Scripts\python -m chaos seed      # uploads 3 synthetic documents and waits until Paperless has processed them
.venv\Scripts\python -m chaos status    # environment + safety status (all checks should PASS)
.venv\Scripts\python -m chaos serve     # dashboard at http://127.0.0.1:8800 (rules only, no AI model)
```

To use **Gemini** instead (recommended for the demo), save your key once as a Windows user variable and start the
dashboard with the helper script:

```powershell
[Environment]::SetEnvironmentVariable('GEMINI_API_KEY', '<your key>', 'User')   # once; never committed
.\serve-gemini.ps1                                                             # instead of `python -m chaos serve`
```

Paperless UI: http://localhost:8000. The user is `chaosadmin`; the password is `PAPERLESS_ADMIN_PASSWORD` in
`sandbox/.env`.

After pulling new code, run `python -m chaos up` again: it rebuilds the helper image (`chaos-netem:local`, now with
`iptables`) and recreates the webserver with the 64 MB upload scratch volume the disk fault needs.

## 3. Demo (repeatable)

Open http://127.0.0.1:8800.

1. **Overview** shows the website as it is: health, live metrics, resource use and the dependency map.
2. On **Scan & inject**, click **Start resilience scan**. The scan streams its steps, then the AI ranks the weak points
   and proposes experiments (about 1 minute).
3. Review the experiments (what is injected, which weak points each tests, when it runs), uncheck any you do not want,
   and click **Approve & inject** (at least 3).
4. Watch section 4: each fault is injected, the self-healing agent detects it, diagnoses it, applies a runbook and
   verifies the fix, with the response-time chart marking each moment. Each experiment takes up to about 2.5 minutes.
5. Section 5 fills in the findings: one row per weak point with its verdict, what happened, how self-healing did and
   the recommended fix.

**Kill switch:** the red **KILL SWITCH** button, `python -m chaos kill` in any terminal, or Ctrl-C in a CLI `run`.
It removes every fault immediately and stops the campaign.

### The dashboard

| Page | What it shows |
|---|---|
| **Overview** | A status bar (overall state, each component with its uptime, the upload disk, and the resilience score); six live metrics with 15-minute trends (requests per minute, error rate, median response with p95, documents stored, upload-to-processed time, task queue); CPU and memory per container from Docker; the dependency map; the effect of the latest experiment on each user workflow before / during / after; and its response-time chart with the event log. |
| **Scan & inject** | The campaign graph; ① the scan log; ② the weak points ranked by the AI, evidence one click away; ③ the experiments with the weak points each tests and when it runs (approval happens here); ④ the live view of the running experiment: the five moments (fault injected, detected, diagnosed, fix applied, back to normal), the chart and workflow table, and the self-healing agent's full detail (measured anomalies and normal signals, AI diagnosis with cited signals and ruled-out alternatives, runbook steps, verification); ⑤ the findings table. |

The top bar shows one status pill (connection, active fault, AI model in use) and the kill switch. The dashboard uses
plain names: `db` is "database", `broker` is "message broker", and probes appear as the workflows they stand for.
Anything produced by the rules fallback instead of the AI is labelled as such.

### Command line

The engine, planner and analyst also work from the CLI:

```powershell
.venv\Scripts\python -m chaos baseline --duration 30                      # baseline under the synthetic workload
.venv\Scripts\python -m chaos run --target db --duration 60 --max-error-rate 0.3 `
    --hypothesis "A hung database is detected and the experiment aborts itself"   # one experiment (automatic abort mode)
.venv\Scripts\python -m chaos runs                                         # recorded runs
.venv\Scripts\python -m chaos report latest                                # one run: measurements + diagnosis + fix
.venv\Scripts\python -m chaos plan                                         # AI planner: ranked experiments
.venv\Scripts\python -m chaos plan-run latest                              # run a plan in sequence (or --ranks 1,3)
.venv\Scripts\python -m chaos score                                        # resilience score, breakdown, coverage
.venv\Scripts\python -m chaos report findings --out findings.txt           # report across all experiments
.venv\Scripts\python -m chaos analyze all                                  # analyst on earlier runs
.venv\Scripts\python -m chaos export all                                   # JSON evidence per experiment in data/exports/
.venv\Scripts\python -m chaos retest <run_id>                              # after applying a fix: rerun and compare
```

Runs started from the CLI use the automatic threshold abort; campaign runs from the dashboard use self-healing mode
(section 5.3).

### AI provider

| `CHAOS_LLM_PROVIDER` | Needs | Model setting (default) |
|---|---|---|
| `openai` | any OpenAI-compatible `/chat/completions` endpoint (Gemini, OpenAI, LM Studio, vLLM) | `CHAOS_OPENAI_MODEL`, `CHAOS_OPENAI_URL`, `OPENAI_API_KEY`, `CHAOS_OPENAI_FALLBACK_MODELS` |
| `anthropic` | `pip install anthropic` and `ANTHROPIC_API_KEY` | `CHAOS_ANTHROPIC_MODEL` (`claude-opus-5-5`) |
| `ollama` | a local [Ollama](https://ollama.com) server | `CHAOS_OLLAMA_MODEL` (`llama3.1`), `CHAOS_OLLAMA_URL` |
| `none` | nothing | deterministic rules only |
| `auto` (default) | first available of Anthropic key, local Ollama, OpenAI key | |

`serve-gemini.ps1` sets the `openai` provider to Google's OpenAI-compatible endpoint, reads `GEMINI_API_KEY` from your
Windows user variables, uses `gemini-3.5-flash` with `gemini-3.1-flash-lite` and `gemini-flash-latest` as fallbacks,
and sets a 40 s model timeout. Hosted models often answer 429/503 when overloaded: the `openai` provider retries once
after 2 s, then tries the fallback models. The dashboard always shows which model actually answered.

When no model is configured, or the model fails or returns something invalid, every AI step falls back to
deterministic rules and is labelled `rules` or `rules-fallback` (with the error). Measurements, detection,
classification and scores never depend on the AI.

## 4. Shutdown and reset

```powershell
.venv\Scripts\python -m chaos down             # rolls back any fault, then compose down (data volumes kept)
.venv\Scripts\python -m chaos down --volumes   # also wipes Paperless data -> clean state
Remove-Item -Recurse data                      # wipes recorded experiments, incidents and campaigns (optional)
```

## 5. How it works

### 5.1 Architecture

```
 dashboard (static JS, polls REST)          CLI (python -m chaos ...)
            │                                      │
            ▼                                      ▼
 ┌──────── chaos controller (FastAPI, 127.0.0.1:8800) ───────────────┐
 │ engine.py      experiment lifecycle (threads), recovery checks    │
 │ safety.py      allowlist validation · thresholds · lock/block/kill│
 │ faults.py      8 fault types + emergency_rollback sweep           │
 │ workload.py    synthetic workload v1 + health sampling            │
 │ monitor.py     always-on probes + website metrics for Overview    │
 │ discovery.py   compose + docker inspect → dependency graph        │
 │ docker_ops.py  ONLY module that runs docker (fixed argv, no shell)│
 │ storage.py     SQLite (data/chaos.sqlite3): samples, runs, events,│
 │                plans, analyses, incidents, campaigns              │
 │ ── campaign (LangGraph) ───────────────────────────────────────── │
 │ scanner.py     read-only resilience scan → findings with evidence │
 │ campaign.py    scan → rank → select → confirm → inject → report   │
 │ ── self-healing (LangGraph; AI only names the failure class) ──── │
 │ detection.py   blind anomaly detection, numbered signals, rules   │
 │ healer.py      observe → detect → diagnose → runbook → fix → verify│
 │ runbooks.py    fixed rule table + allowlisted fix actions         │
 │ grading.py     grades the agent afterwards; circuit-breaker check │
 │ ── analysis (proposes and interprets; never executes) ─────────── │
 │ catalog.py     the only experiments and workflows agents may use  │
 │ planner.py     AI planner for the CLI flow, strictly validated    │
 │ orchestrator.py runs approved plan items in sequence              │
 │ evidence.py / analysis.py  evidence record, classification, facts │
 │ analyst.py     AI analyst: diagnosis grounded in numbered facts   │
 │ scoring.py     deterministic severity and resilience scores       │
 │ llm.py         provider interface (OpenAI-compatible / Anthropic / Ollama) │
 └───────────────┬────────────────────────────────────┬──────────────┘
                 │ docker CLI (sandbox-only)          │ HTTP (real Paperless API)
                 ▼                                    ▼
   compose project "paperless-chaos"     webserver ─▶ db (postgres:18)
   (label chaos.sandbox=true)                    ├─▶ broker (valkey:9)
                                                 └─▶ scratch volume (64 MB tmpfs)
                 ▲
 watchdog.py: a separate detached process per experiment that removes the fault at the
 hard deadline if the controller has not (dead-man's switch)
```

### 5.2 Chaos campaign (`scanner.py`, `campaign.py`)

One LangGraph graph with a human-in-the-loop interrupt:

```
scan -> rank (AI) -> select (AI + safety policy) -> confirm (you) -> inject & heal (agent + tool) -> ... -> report
```

1. **Scan** (read-only, streamed live): discovers the architecture; sends live requests to every workflow; inspects
   replicas, restart policies, health checks and CPU / memory limits; checks the database timeout setting and the
   latency headroom against the 5 s client timeout; looks for circuit breakers; and measures the upload scratch
   disk. Each weak point becomes a finding (R1, R2, …) with its evidence and the experiments that would test it.
2. **Rank and select (AI).** The model ranks every finding with a rationale and proposes experiments so that **every**
   finding is tested (at least 3; one experiment may cover several findings). Each must come from the tests the scan
   suggested for that finding and pass the safety policy. The AI may make an experiment **stronger** than suggested
   (more latency or loss, a tighter CPU or memory limit) but never milder, so every injected fault really bites.
   Anything else is rejected and listed; uncovered findings are topped up by rule; rules take over if the model fails.
3. **Confirm (you).** The graph pauses until you approve at least 3. You can uncheck experiments or cancel.
4. **Inject & heal.** For the next approved experiment, the execution agent calls the `inject_fault` tool (a LangChain
   structured tool). The tool refuses any call that does not match an approved, pending experiment exactly; a refused
   AI call is recorded and the approved experiment runs instead. Each run goes through the engine in self-healing
   mode, the self-healing agent resolves it, and the analyst diagnoses it.
5. **Report.** For every experiment: before / during / after error rate and p95, the agent's timings and graded
   diagnosis, the analyst's impact, severity and fix, and a measured **circuit-breaker check** (do failed requests
   get faster as the outage goes on, or does each one wait for its timeout?). For every weak point: confirmed or not.

API: `POST /api/campaign`, `GET /api/campaign/latest`, `GET /api/campaign/{id}`,
`POST /api/campaign/{id}/confirm {"approved": ["E1","E2","E3"]}`, `POST /api/campaign/{id}/cancel`.

### 5.3 Self-healing agent (`healer.py`, `detection.py`, `runbooks.py`)

```
observe -> detect --(anomalies confirmed)--> diagnose -> select_runbook -> remediate -> verify -> close
   ^         | healthy                          ^                                         |
   +---------+                                  +------ not fixed: exclude class, retry --+  (max 2, then escalate)
```

| Step | Decided by | What happens |
|---|---|---|
| observe | - | Reads probe results (by timestamp only) and container states. Learns a healthy baseline from the first 12 s. It is given only the run id: never the fault type, the target or the injection time (a test fails if it reads the run's config). |
| detect | statistics | Over the last 15 s, a workflow is anomalous when its error rate is ≥ 50 % and ≥ 40 points above normal (≥ 2 failures), or its successful-request p95 is ≥ 2.5× normal and ≥ 800 ms slower. Paused, stopped or unhealthy containers and Paperless' own status report ("redis=ERROR") are signals too. An incident opens when anomalies persist on 2 consecutive checks. |
| diagnose | **AI** (rules if it fails) | Waits up to 6 s for workflow-level evidence, then takes a fresh snapshot with live container state, CPU and memory use against limits, and upload-disk usage. Anomalies become numbered signals (A1, A2, …) and normal ones N1, N2, …; failures are marked fast (rejected) or slow (hanging). The model must pick one of 11 classes and cite signal ids in every reasoning step; uncited steps, unknown ids, extra fields or excluded classes are rejected (one retry). A rules classifier on the same signals is the fallback and a cross-check. |
| select_runbook | **fixed rule table** | See below. The AI cannot choose or change the action. |
| remediate | the runbook | One fixed action through `docker_ops`, allowed only on a service where the safety policy permits the fault that action undoes. |
| verify | checks | The target must be healthy and the application must answer normally within 60 s. If not, the class is excluded and the agent diagnoses again. |

| Failure class | Runbook |
|---|---|
| database / broker down (frozen, hung or stopped) | RB-1 / RB-2: unpause or start the container |
| database / broker network degraded (delay or loss) | RB-3 / RB-4: remove the traffic-control rule |
| database / broker starved of CPU | RB-5 / RB-6: lift the CPU quota |
| database / broker refusing connections | RB-7 / RB-8: remove the connection-reject rule |
| database / broker out of memory | RB-9 / RB-10: recreate the container from its compose definition |
| upload disk full | RB-11: delete the file filling the scratch disk |
| unknown | RB-0: escalate to a human, change nothing |

In self-healing mode the engine holds the fault instead of aborting on thresholds (breaches are still recorded), for
up to 90 s. The engine's safety net still applies: the planned-end removal, the independent watchdog and the kill
switch. When the agent reports a fix, the engine checks the real fault state itself before accepting it, so "removed
by the agent" is never the agent's own claim. Afterwards `grading.py` compares the diagnosis with what was actually
injected: correct class, time to detect, diagnose, fix and recover, and who removed the fault. A run with no
user-facing impact and no incident is graded "correctly stayed quiet", not "missed".

Campaign runs always use self-healing mode. For runs started through the API, it is on by default under `serve`
(`GET/POST /api/agent`). Incidents: `GET /api/incidents`, `GET /api/incidents/latest`, `GET /api/incidents/{id}`.

### 5.4 Fault catalogue (`faults.py`)

| Fault | Targets | Parameter | Injection | Removal, verified by |
|---|---|---|---|---|
| `dependency_pause` | db, broker | - | `docker pause` (the process hangs; connections stay open) | `docker unpause`; inspect shows running, not paused |
| `dependency_stop` | db, broker | - | `docker stop` (restart policy `unless-stopped` keeps it down) | `docker start`; inspect shows running |
| `network_latency` | db, broker | `latency_ms` 10–3000 | helper sidecar in the target's network namespace: `tc netem delay`, self-expiring | remove sidecar + `tc qdisc del`; fresh `tc qdisc show` has no netem |
| `packet_loss` | db, broker | `loss_pct` 1–60 | same sidecar: `tc netem loss` | same as latency |
| `cpu_exhaustion` | db, broker | `cpu_limit` 0.02–0.5 CPUs | `docker update --cpu-period/--cpu-quota` | `docker update --cpu-quota -1`; no CPU quota left |
| `connection_refused` | db, broker | - | helper sidecar: `iptables` rejects every TCP packet to port 5432 / 6379 (clients fail at once) | remove sidecar + delete the `CHAOS` chain; `iptables -S` is clean |
| `memory_exhaustion` | db, broker | `memory_mb` 6–256 | `docker update --memory/--memory-swap` below the working set (OOM kills, restarts) | recreate from the compose definition (Docker cannot lift a memory limit); no limit left |
| `disk_exhaustion` | webserver (scratch volume only) | - | fill Paperless' 64 MB tmpfs upload scratch volume to 100 % (uploads fail, reads work) | delete the fill file; usage below 90 % |

The webserver container itself is never disturbed; its only permitted fault fills the bounded scratch volume, which
lives in memory and cannot touch the host disk. Injection of the disk fault is checked (the volume must really reach
≥ 95 %), and a fault's start time is taken when injection begins, so a slow `docker stop` is not mis-timed.

### 5.5 Experiment lifecycle (`engine.py`)
1. **Validate** the config. Pydantic rejects unknown fields and malformed names; `safety.validate_config` enforces the
   allowlist, the per-fault parameters and the limits. Rejections are recorded.
2. **Preflight**: Docker reachable; webserver, db and broker running, unpaused and healthy; no leftover sidecars; the
   target resolves to a container in the project carrying `chaos.sandbox=true`; fault-specific checks (helper image,
   no existing rule or quota or limit, scratch volume present and not full); real app probes succeed.
3. **Baseline**: 15 s of the workload immediately before injection.
4. **Inject**. The active-fault marker is persisted first, then the fault is applied and a watchdog process is started
   (deadline = planned end + 10 s, + 60 s for memory faults, whose removal recreates the container). Every second
   the engine evaluates rolling-window error rate and p95 latency and checks for a kill request; in self-healing mode
   it also checks whether the agent reported a fix.
5. **Rollback** (`finally`): remove the fault (with retries) and verify removal against the real container state,
   qdisc, iptables rules, limits or disk usage. If removal can't be verified, an emergency sweep runs; if that also
   fails, experiments are **BLOCKED**.
6. **Recovery check**: the target is healthy and N consecutive `api_documents` and `api_status` probes succeed. If not
   confirmed within the timeout, experiments are **BLOCKED**.
7. **Post-observation**: 10 s more of the workload, while in-flight document processing drains.
8. **Persist** the config, timestamps, per-phase metrics, impact, failed checks, filtered logs, events and outcome.

### 5.6 Analysis and scoring

**Evidence (`evidence.py`, `analysis.py`).** Python compares the fault phase with the baseline for each workflow and
classifies it: `failed` (error rate ≥ 50 % and 40 points above baseline), `degraded` (more than 10 points above
baseline, or p95 more than 2× and 200 ms slower), `functional`, or `no_data`. Impact is `full`, `partial`, `none` or
`inconclusive`. The result is a numbered list of facts (F1, F2, …).

**Analyst (`analyst.py`).** The model sees the facts, the classification and the dependency edges, and returns a
summary, ranked root-cause hypotheses with supporting and contradicting fact ids, the weakness, one recommended fix
with its category, verification criteria, confidence and limitations. Citations of non-existent facts, unknown
components or unsupported root causes are rejected. Container logs are passed as quoted, untrusted data. Fixes are
recommendations only; nothing is applied to the application.

**Planner (`planner.py`, CLI flow).** Ranks experiments from the dependency map, the capability catalogue and earlier
results; every recommendation is validated against the same schema and safety policy as a user request.

**Scoring (`scoring.py`).** Scores are computed, never taken from the model.

| Severity component (0–100, higher is worse) | Weight | Normalization |
|---|---|---|
| Breadth | 30 % | share of measured workflows affected (failed = 1, degraded = 0.5) |
| Criticality | 25 % | the same share weighted by workflow criticality |
| Magnitude | 25 % | max of the critical error-rate increase and the slowdown of successful requests |
| Recovery | 20 % | recovery time / 60 s capped at 1; 1.0 if rollback or recovery is unverified |

Categories: critical ≥ 75, high ≥ 50, medium ≥ 25, low > 5. An unverified rollback or recovery floors severity at 90.

| Resilience component (0–100, higher is better) | Weight | Normalization |
|---|---|---|
| Availability | 50 % | 1 − criticality-weighted error-rate increase during the fault |
| Performance | 20 % | 1 − mean slowdown of successful requests |
| Recovery | 30 % | 1 − recovery time / 60 s; 0 if unverified |

The overall score is the mean of the latest scored run per (fault, target) scenario, shown with its coverage out of
the 15 catalogue scenarios. If any scenario's latest run is unrecovered, the score is capped at 25. Workflow
criticality weights (`catalog.py`): api_documents 0.30, doc_upload 0.25, web_root 0.20, doc_processing 0.20,
api_status 0.05.

### 5.7 Synthetic workload v1 and website metrics

| Probe | Call | Every | Exercises |
|---|---|---|---|
| `web_root` | `GET /` | 2 s | web server, session / DB |
| `api_documents` | `GET /api/documents/` | 2 s | DB read path |
| `api_status` | `GET /api/status/` | 4 s | Paperless' own DB + Redis checks |
| `doc_upload` | `POST /api/documents/post_document/` (unique `.txt`) | 8 s | scratch disk, Redis/Celery enqueue |
| `doc_processing` | upload → task `success` | per upload | worker, parser, DB write (end-to-end) |

Every probe has a 5 s timeout and runs on its own thread, so a hung dependency shows up as timed-out probes rather
than stalling the workload.

Between experiments, `monitor.py` probes the app every 5 s and, every 15 s, reads the document count, the task queue
and `docker stats`; every 30 s it measures the upload scratch disk. `GET /api/metrics` turns this into the Overview's
metrics with 15-minute trends.

## 6. Safety boundaries

- **Allowlist** (`config.py`): `db` and `broker` (seven faults each) and the webserver's scratch volume (disk fault
  only) in compose project `paperless-chaos`. The host and every other container are rejected. Every container
  operation re-checks the project label **and** `chaos.sandbox=true`; the scratch volume must carry the sandbox label
  and be a tmpfs.
- **No arbitrary commands**: the API accepts only `ExperimentConfig` (extra fields → 422). Docker is invoked with
  fixed argv lists from `docker_ops.py`, never a shell. Compose subcommands are allowlisted.
- **Hard limits**: duration 5–120 s, latency 10–3000 ms, packet loss 1–60 %, CPU 0.02–0.5 CPUs, memory 6–256 MB,
  threshold window 3–60 s, recovery timeout ≤ 300 s. Values outside are rejected, not clamped.
- **Automatic abort** on a rolling-window error rate or p95 breach (CLI runs). In self-healing mode the fault is held
  for the agent instead, still bounded by its planned duration.
- **Kill switch** (dashboard, `POST /api/kill`, `python -m chaos kill`, Ctrl-C): sets the abort flag in the process and
  in SQLite, stops the campaign and the agent, **and** immediately runs an emergency sweep that undoes every fault type.
- **Independent watchdog**: a detached OS process removes the fault after the deadline if the controller hasn't. The
  netem, iptables and disk sidecars also expire on their own.
- **Crash recovery**: on startup, a leftover active-fault marker triggers rollback and verification; orphaned runs are
  marked `interrupted`.
- **Block on uncertainty**: unverified removal or recovery sets a persisted BLOCKED flag. New experiments get 423
  until `unblock` re-verifies the whole environment.
- **One experiment at a time**: a thread lock plus a SQLite lease with a heartbeat, across the API and CLI processes.
- **Least privilege**: Paperless is published on 127.0.0.1 only; db and broker publish no ports. All containers run
  with `no-new-privileges`. Network sidecars run with `--cap-drop ALL --cap-add NET_ADMIN`, 32 MB of memory and 16
  pids; the disk filler has no network and no capabilities. The controller binds 127.0.0.1. Secrets are generated per
  sandbox and gitignored; API keys are read from environment variables, never stored in the repo.
- **AI boundaries**: the AI ranks, proposes, names failure classes and writes diagnoses. Every output is validated
  against a strict schema, the catalogue and the safety policy, and must cite measured evidence. The only tool the AI
  can call, `inject_fault`, runs only an experiment a human approved, with exactly the approved parameters. Fixes are
  chosen by a fixed rule table and executed by fixed actions. A provider outage or malformed reply falls back to
  rules and is recorded; it cannot delay or skip rollback.

## 7. Tests

```powershell
.venv\Scripts\python -m pytest -q        # 178 tests
```

The tests use in-memory fakes for Docker and Paperless (`tests/fakes.py`) whose app breaks the way the real one does
for every fault type. They cover:
- policy validation: injection attempts, extra fields, limits, per-fault parameters, the webserver's disk-only rule
- threshold maths, the full lifecycle (completion, abort, kill switch, injection failure, unverifiable removal →
  BLOCKED → unblock, unrecovered app), the cross-process lock, crash recovery and the watchdog
- every fault's injection, removal and verification, and the emergency sweep (including never touching the webserver)
- dependency discovery, the API (rejections, preflight, kill, metrics)
- the planner, evidence analysis, the analyst, scoring and the findings report
- detection: errors vs timeouts vs slowness, fast (refused) vs slow (hung) failures, CPU, memory and disk signals,
  one-off blips ignored
- the AI diagnosis contract (missing or unknown citations, excluded classes, extra fields) and the rules classifier
- every runbook, including refusal of actions on services where they are not permitted
- the full self-healing graph: correct fixes for pause, stop, connection refusal, memory and disk; a wrong first
  diagnosis caught by verification and retried; an AI outage falling back to rules; escalation with the engine's
  deadline still removing the fault; blindness (the agent never reads the run's config)
- the scan, ranking and selection validation (grounding, coverage of every finding, no milder parameters), and the
  campaign end to end: waiting for confirmation, refusing fewer than 3 or unknown approvals, running only what was
  approved, refusing an AI tool call outside the approval, and cancelling at confirmation

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `docker CLI not found` / `Docker engine not reachable` | Start Docker Desktop and wait for "Engine running"; or set `CHAOS_DOCKER_BIN` to the full path of `docker.exe` |
| `compose up` times out on first start | The Paperless image is large; run `docker compose -p paperless-chaos -f sandbox/docker-compose.yml pull`, then `python -m chaos up` again |
| Preflight `webserver running, not paused, healthy` fails | Paperless is still migrating on first boot (start period 120 s). Wait, then `python -m chaos status` |
| Preflight `app probe api_documents` times out | The sandbox is slow or busy (for example while tests run). Wait and retry; give Docker Desktop more CPU / memory |
| Preflight fails on `helper image` or `iptables` | `python -m chaos up` rebuilds `chaos-netem:local` with `iptables` (needs Docker Hub access for `alpine:3.20`) |
| Preflight fails on `bounded scratch volume present` | `python -m chaos up` recreates the webserver with the `paperless-chaos_scratch` volume |
| AI steps say `rules-fallback` with HTTP 429 / 503 | The hosted model is overloaded. `serve-gemini.ps1` already falls back to other Gemini models; set `CHAOS_OPENAI_MODEL` to a model that answers reliably |
| Dashboard banner **Experiments blocked** | Run `python -m chaos rollback`, check `docker ps`, then click "Verify environment & unblock" (or `python -m chaos unblock`) |
| A container stays paused, stopped or limited after a crash | `python -m chaos rollback` (works without the server) sweeps every fault type |
| Port 8000 or 8800 in use | Change the published port in `sandbox/docker-compose.yml` and set `CHAOS_PAPERLESS_URL`; or `python -m chaos serve --port 8801` |
| Token errors | Delete the Paperless data with `python -m chaos down --volumes`, then `up` (the admin is created on first boot from `sandbox/.env`) |

## 9. Known limitations

- One target per experiment. Faults cover the database, the broker and the upload scratch disk; third-party API
  outages are not implemented (Paperless calls no external API in this sandbox).
- The catalogue has 15 scenarios; the resilience score says nothing about faults outside it.
- Circuit breakers are validated by measurement only (do failures get faster during an outage?); no breaker is added.
- Memory faults are fixed by recreating the container, which takes about 20 s; together with detection and diagnosis
  the fix can land after the planned end, in which case the engine removes the fault and the agent is not credited.
- The AI sometimes answers "unknown" for connection refusal instead of "refusing connections"; the agent then
  escalates and the engine removes the fault at the planned end. The rules classifier handles this case.
- Detection relies on the synthetic workload, so it needs traffic. With a slow sandbox (probe p95 near the 5 s
  timeout) the baseline itself contains timeouts, which raises the bar for an anomaly.
- Runbooks undo the injected faults (unpause, start, clear rules, lift limits, recreate, free disk); real incidents
  would also need runbooks such as failover or scaling.
- Root causes are hypotheses drawn from black-box measurements; no Paperless code is inspected. Workflow-to-service
  expectations in `catalog.py` are assumptions from the architecture.
- The watchdog guarantee is best-effort: if Docker itself is down, nothing can undo a fault, and the controller
  reports BLOCKED. `docker pause` needs Linux containers (the Docker Desktop default).
- Metrics come from the synthetic workload, the Paperless API and `docker stats`; there is no Prometheus or cAdvisor.
- Recommended fixes are not applied automatically; `retest` compares before and after once you have applied one.
