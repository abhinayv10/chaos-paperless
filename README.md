# Paperless-ngx Chaos Lab

An AI-driven chaos-engineering prototype for the Bosch hackathon. It runs
[Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) (GPL-3.0) in an isolated Docker Compose
sandbox. It then injects one bounded, reversible failure into a dependency and measures the effect on
real application workflows. Safety controls that work independently of the UI abort the experiment
automatically, roll it back and verify recovery.

The method follows the [`testing-chaos`](https://www.skills.sh/wojons/skills/testing-chaos) skill:
- hypothesis-driven experiments
- a contained blast radius
- baseline metrics before injection
- small, controlled faults
- automatic rollback and abort, plus a manual abort
- verification after the experiment
- a results report in the skill's output format (`python -m chaos report`)

On top of that engine sits a closed loop with two AI agents (sections 3 and 6):

1. **Discover** the application's dependencies from Compose and the running containers.
2. **Plan**: the Chaos Planner ranks experiments worth running and says why.
3. **Execute** the approved ones, one at a time, through the safety-controlled engine.
4. **Analyse**: the Analyst diagnoses each run from measured evidence and recommends a fix.
5. **Score** severity and resilience deterministically, and produce a findings report.
6. **Retest** after a human applies a fix.

The agents only propose and interpret. They cannot run commands, touch Docker or change the application.

---

## 1. Prerequisites

| Need | Notes |
|---|---|
| Docker Desktop (WSL2 backend) or Docker Engine + Compose v2 | `docker version` must show a **Server** section |
| Python 3.11+ | tested with 3.13 |
| ~3 GB disk for images | Paperless image is about 1.5 GB |

On Windows: install WSL2 (`wsl --install`, then reboot), then install and start Docker Desktop.

## 2. Setup and startup

```powershell
cd chaos-paperless
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\python -m chaos up        # generates sandbox/.env secrets, builds the netem helper, starts the stack and waits until healthy
.venv\Scripts\python -m chaos seed      # real workflow: upload 3 synthetic docs, wait until Paperless has processed them
.venv\Scripts\python -m chaos status    # environment + safety status (all checks should PASS)
.venv\Scripts\python -m chaos serve     # dashboard at http://127.0.0.1:8800
```

Paperless UI: http://localhost:8000. The user is `chaosadmin`, and the password is in `sandbox/.env`.

## 3. Demo script (repeatable)

With `serve` running, use the dashboard. You can also run the same steps from a second terminal:

```powershell
# 1. baseline under the fixed synthetic workload (v1)
.venv\Scripts\python -m chaos baseline --duration 30

# 2. a fault with visible impact that completes its planned duration (the broker pause breaks uploads/processing, but reads keep working)
.venv\Scripts\python -m chaos run --target broker --duration 20 --max-error-rate 0.6 `
    --hypothesis "Reads keep working while the broker is frozen; uploads stall and recover after unpause"

# 3. a threshold breach that triggers an automatic abort (the db pause makes every API call hang; abort at 30% errors)
.venv\Scripts\python -m chaos run --target db --duration 60 --max-error-rate 0.3 `
    --hypothesis "A hung database is detected and the experiment aborts itself within seconds"

# 4. inspect the results
.venv\Scripts\python -m chaos runs
.venv\Scripts\python -m chaos report latest
```

Kill switch: the red **KILL SWITCH** button in the dashboard, `python -m chaos kill` in any terminal, or Ctrl-C in a
CLI `run`.

### AI-driven loop

The dashboard (http://127.0.0.1:8800) shows the same loop in three sections:

- **Overview**: a headline status (Ready, Running, Fault active, Recovering, Recovered, Failed, Blocked) with one
  sentence saying what happened, the resilience score, and a five-step strip for the current or latest experiment:
  baseline, fault injected, impact observed, rolled back, recovered. Below it are the dependency map, the effect on
  each user workflow before, during and after the fault, and the response-time chart.
- **Experiments**: the same five-step strip, then the ranked plan with what each experiment is expected to affect
  and why it has that rank, one button to approve and run the selected ones, and each completed experiment's
  impact, severity and recovery. Run history and the manual experiment form are in collapsed sections below.
- **Findings**: the score with its three components, a worst-first table of the weaknesses found, and one card per
  weakness: what broke (measured), the likely cause, and the recommended fix, with the evidence one click away.

The dashboard uses plain names: `db` is shown as "database", `broker` as "message broker", and the probes as the
workflows they stand for ("List documents" for `api_documents`, and so on).

"Recovered" is shown only once the controller has verified recovery. Output from the rules fallback is labelled as
rule-based, with a notice on the plan and on the findings, and the top bar shows whether an LLM is configured.

```powershell
.venv\Scripts\python -m chaos plan                  # Agent 1: ranked experiments with hypothesis and reasons
.venv\Scripts\python -m chaos plan-run latest       # run the plan in sequence (or --ranks 1,3); each run is analysed
.venv\Scripts\python -m chaos score                 # overall resilience, breakdown and coverage
.venv\Scripts\python -m chaos report latest         # one run: measurements + diagnosis + recommended fix
.venv\Scripts\python -m chaos report findings --out findings.txt   # final report across all experiments

.venv\Scripts\python -m chaos analyze all           # Agent 2 on runs recorded earlier (run id | latest | all)
.venv\Scripts\python -m chaos export all            # one JSON evidence file per experiment in data/exports/
.venv\Scripts\python -m chaos retest <run_id>       # after you apply a fix: same config again, then a comparison
```

Running `plan-run` (or clicking **Approve & run selected** on the Experiments tab) is the human approval step. Nothing the
planner proposes starts by itself.

### AI provider

| `CHAOS_LLM_PROVIDER` | Needs | Model setting (default) |
|---|---|---|
| `ollama` | a local [Ollama](https://ollama.com) server | `CHAOS_OLLAMA_MODEL` (`llama3.1`), `CHAOS_OLLAMA_URL` |
| `anthropic` | `pip install anthropic` and `ANTHROPIC_API_KEY` | `CHAOS_ANTHROPIC_MODEL` (`claude-opus-5-5`) |
| `openai` | any OpenAI-compatible `/chat/completions` endpoint | `CHAOS_OPENAI_MODEL`, `CHAOS_OPENAI_URL`, `OPENAI_API_KEY` |
| `none` | nothing | deterministic rules only |
| `auto` (default) | first available of Anthropic key, local Ollama, OpenAI key | |

You can also pass `--provider` to `plan`, `plan-run`, `analyze` and `retest`. With no provider, or when the provider
fails or returns something invalid, both agents fall back to deterministic rules. Their output is then labelled
`rules` or `rules-fallback` (with the provider's error) everywhere it appears, so it is never mistaken for an AI
diagnosis. Measurements, classification and scores do not depend on the provider at all.

## 4. Shutdown and reset

```powershell
.venv\Scripts\python -m chaos down             # rolls back any fault, then compose down (data volumes kept)
.venv\Scripts\python -m chaos down --volumes   # also wipes Paperless data -> clean state
Remove-Item -Recurse data                      # wipes recorded experiments (optional)
```

## 5. Architecture

```
 dashboard (static JS, polls REST)          CLI (python -m chaos ...)
            │                                      │
            ▼                                      ▼
 ┌──────── chaos controller (FastAPI, 127.0.0.1:8800) ───────────────┐
 │ engine.py     experiment lifecycle (threads), recovery checks     │
 │ safety.py     allowlist validation · thresholds · lock/block/kill │
 │ faults.py     PauseFault · LatencyFault · emergency_rollback      │
 │ workload.py   synthetic workload v1 + health sampling             │
 │ monitor.py    always-on light probes for the Overview             │
 │ discovery.py  compose + docker inspect → dependency graph         │
 │ docker_ops.py ONLY module that runs docker (fixed argv, no shell) │
 │ storage.py    SQLite (data/chaos.sqlite3): samples, runs, events, │
 │               plans, analyses                                     │
 │ ── AI loop (proposes and interprets; never executes) ──────────── │
 │ catalog.py      the only experiments and workflows agents may use │
 │ planner.py      Agent 1: ranked plan, strictly validated          │
 │ orchestrator.py runs approved items in sequence via engine.start  │
 │ evidence.py     structured record per run + JSON export           │
 │ analysis.py     deterministic comparison, classification, facts   │
 │ scoring.py      deterministic severity and resilience scores      │
 │ analyst.py      Agent 2: diagnosis grounded in numbered facts     │
 │ llm.py          provider interface (Ollama / Anthropic / OpenAI)  │
 └───────────────┬────────────────────────────────────┬──────────────┘
                 │ docker CLI (sandbox-only)          │ HTTP (real Paperless API)
                 ▼                                    ▼
   compose project "paperless-chaos"     webserver ─▶ db (postgres:18)
   (label chaos.sandbox=true)                       └▶ broker (valkey:9)
                 ▲
 watchdog.py: a separate detached process per experiment that removes the fault at the
 hard deadline if the controller has not (dead-man's switch)
```

### Experiment lifecycle (`engine.py`)
1. **Validate** the config. Pydantic rejects unknown fields and malformed names, and `safety.validate_config`
   enforces the allowlist and limits. Rejections are recorded.
2. **Preflight**: Docker reachable; webserver, db and broker running, unpaused and healthy; no leftover fault
   sidecars; the target resolves to a container in the project that carries `chaos.sandbox=true`; target-specific
   checks; and real app probes (`/api/status/`, `/api/documents/`) succeed.
3. **Baseline**: 15 s of the workload immediately before injection.
4. **Inject**. The active-fault marker is persisted first, then the fault is applied and a watchdog process is
   started. Every second the engine evaluates rolling-window error rate and p95 latency over the fault-phase
   samples, and checks for a kill request.
5. **Rollback** (`finally`): remove the fault (with retries), then verify removal by inspecting the actual
   container state or qdisc. If removal can't be verified, an emergency sweep runs, and if verification still
   fails, experiments are **BLOCKED**.
6. **Recovery check**: the target is healthy and N consecutive `api_documents` and `api_status` probes succeed.
   The recovery time is measured from fault removal. If recovery isn't confirmed within the timeout,
   experiments are **BLOCKED**.
7. **Post-observation**: 10 s more of the workload, while in-flight document processing drains.
8. **Persist** the config, timestamps, per-phase metrics, impact comparison, failed checks, filtered container
   logs, safety events and the outcome.

### AI loop

**Planner (`planner.py`).** It receives the dependency map from `discovery.py`, the capability catalogue (fault types,
permitted targets, parameter bounds, all derived from the safety limits), the workflow table and a summary of earlier
experiments. It returns ranked recommendations: fault, target, bounded parameters, hypothesis, reason for the rank,
expected effects, dependencies, workflows and risks. Each one is parsed with a strict schema (unknown fields are
rejected), converted to an `ExperimentConfig` and checked with `safety.validate_config`, the same policy the engine
uses. Unknown fault types, unauthorized targets, out-of-range values, unknown workflows or services and duplicates are
dropped and listed under "rejected by validation". One retry is made with the errors; after that the rules ranking is
used. The rules ranking scores each scenario by the criticality of the workflows that pass through the target, then
adjusts for evidence: untested scenarios and ones that showed a weakness move up, harmless ones move down.

**Sequencing (`orchestrator.py`).** Approved items run one at a time through `Engine.start`, so every run gets fresh
validation and preflight, its own baseline, thresholds, watchdog, rollback and recovery check. There is no second
injection or rollback path. The sequence stops if an item is rejected, ends `failed` or `interrupted`, leaves
recovery unverified or the environment BLOCKED, or when the kill switch is used. Analysis and export happen only after
a run has fully finished, and an error there is recorded without affecting the sequence.

**Evidence (`evidence.py`, `analysis.py`).** The evidence record is built from SQLite on demand; the JSON export is a
rendering of it, not a second copy. Python then compares the fault phase with the baseline for each workflow and
classifies it: `failed` (error rate at least 50% and 40 points above baseline), `degraded` (more than 10 points above
baseline, or p95 more than 2x and 200 ms slower), `functional`, or `no_data`. Impact is `full`, `partial`, `none` or
`inconclusive` (no fault injected, no baseline, or fewer than 3 critical probes finished during the fault). The
result is a numbered list of facts (F1, F2, ...).

**Analyst (`analyst.py`).** The model sees the facts, the computed classification and the dependency edges, and
returns a summary, ranked root-cause hypotheses with supporting and contradicting fact ids, the likely weakness, one
recommended fix with its category, verification steps with measurable success criteria, confidence and limitations.
Validation rejects any diagnosis that cites a fact id that does not exist, names a component that is not a service,
or gives a root cause with no supporting fact. When the evidence is insufficient the model is not asked at all and the
finding is marked inconclusive. Container logs are passed as quoted, untrusted data. Fixes are recommendations only:
nothing is applied to the application.

**Scoring (`scoring.py`).** Scores are computed, never taken from the model.

| Severity component (0-100, higher is worse) | Weight | Normalization |
|---|---|---|
| Breadth | 30% | share of measured workflows affected (failed = 1, degraded = 0.5) |
| Criticality | 25% | the same share weighted by workflow criticality |
| Magnitude | 25% | max of the critical error-rate increase and the slowdown of successful requests, (p95 ratio - 1) / 9 capped at 1 |
| Recovery | 20% | recovery time / 60 s capped at 1; 1.0 if rollback or recovery is unverified |

Categories: critical >= 75, high >= 50, medium >= 25, low > 5. An unverified rollback or recovery floors severity at 90.

| Resilience component (0-100, higher is better) | Weight | Normalization |
|---|---|---|
| Availability | 50% | 1 - criticality-weighted error-rate increase during the fault |
| Performance | 20% | 1 - mean slowdown of successful requests |
| Recovery | 30% | 1 - recovery time / 60 s; 0 if unverified |

A component with no data is dropped and the other weights renormalized; it is reported as missing, not as perfect.
The overall score is the mean of the latest scored run per (fault, target) scenario. Inconclusive runs are counted
but not scored, and a run cut short by the kill switch never replaces an earlier scored run. If any scenario's latest
run is unrecovered, the overall score is capped at 25. The score is always shown with its coverage (scenarios scored
out of 4) and the run counts by status. Workflow criticality weights are in `catalog.py`: api_documents 0.30,
doc_upload 0.25, web_root 0.20, doc_processing 0.20, api_status 0.05.

### Synthetic workload v1 (`workload.py`)
| Probe | Call | Every | Exercises |
|---|---|---|---|
| `web_root` | `GET /` | 2 s | web server, session/DB |
| `api_documents` | `GET /api/documents/` | 2 s | DB read path |
| `api_status` | `GET /api/status/` | 4 s | Paperless' own DB + Redis checks |
| `doc_upload` | `POST /api/documents/post_document/` (unique `.txt`) | 8 s | Redis/Celery enqueue |
| `doc_processing` | upload → task `success` | per upload | worker, parser, DB write (end-to-end) |

Every probe has a 5 s timeout and runs on its own thread, so a hung dependency shows up as failed, timed-out
probes rather than stalling the workload.

### Fault types
| Fault | Mechanism | Removal | Removal verified by |
|---|---|---|---|
| `dependency_pause` (primary) | `docker pause` on db or broker (cgroup freezer, so the process hangs while its connections stay open) | `docker unpause` (idempotent) | `docker inspect` State.Paused == false and Running |
| `network_latency` (second) | short-lived `chaos-netem:local` sidecar in the target's network namespace runs `tc qdisc ... netem delay` and **removes it itself** after the planned duration plus grace | kill sidecar + `tc qdisc del` | fresh sidecar `tc qdisc show` contains no `netem`, and no sidecars remain |

## 6. Safety boundaries

- **Allowlist** (`config.py`): only services `db` and `broker` in compose project `paperless-chaos`. The webserver,
  the host and every other container are rejected. Every container operation re-checks the project label **and**
  `chaos.sandbox=true`.
- **No arbitrary commands**: the API accepts only `ExperimentConfig` (extra fields → 422). Docker is invoked with
  fixed argv lists from `docker_ops.py`, never a shell. Compose subcommands are allowlisted.
- **Hard limits**: duration 5–120 s, latency 10–3000 ms, threshold window 3–60 s, recovery timeout ≤ 300 s. Values
  outside these ranges are rejected, not clamped.
- **Automatic abort** on a rolling-window error rate or p95 latency breach (configurable per experiment).
- **Kill switch** (dashboard, `POST /api/kill`, `python -m chaos kill`, Ctrl-C): sets the abort flag in the
  process and in SQLite (so any controller process sees it), **and** immediately runs an emergency sweep. It
  doesn't wait for the experiment thread.
- **Independent watchdog**: a detached OS process removes the fault at planned end + 10 s if the controller hasn't
  recorded removal. This covers a crashed or hung controller. The latency sidecar also expires on its own.
- **Crash recovery**: on startup, a leftover active-fault marker triggers rollback and verification, and orphaned
  runs are marked `interrupted`.
- **Block on uncertainty**: unverified removal or recovery sets a persisted BLOCKED flag. New experiments get 423
  until `unblock` re-verifies the whole environment.
- **One experiment at a time**: a thread lock plus a SQLite lease with a heartbeat. This works across the API
  server and CLI processes, and a lease left by a dead process expires after 60 s.
- **Least privilege**: Paperless is published on 127.0.0.1 only, and db and broker publish no ports. All containers
  run with `no-new-privileges`. The netem sidecar runs with `--cap-drop ALL --cap-add NET_ADMIN`, 32 MB of memory,
  a 16-pid limit and `--rm`. The controller binds 127.0.0.1. Secrets are generated per sandbox and gitignored.
- The dashboard is a pure client: closing the browser does not affect a running experiment.
- **AI boundaries**: the agents have no tools. The planner can only pick from the catalogue, and its output goes
  through the same schema and safety policy as a request typed by a user, again immediately before each run. The
  analyst can only cite measured facts. A provider outage, refusal or malformed reply falls back to rules and is
  recorded; it cannot delay or skip rollback, because the AI is only called before a run starts or after it has
  finished. The kill switch also stops a running sequence. No AI-suggested fix is ever applied automatically.

## 7. Tests

```powershell
.venv\Scripts\python -m pytest -q
```
These tests use in-memory fakes for Docker and Paperless (`tests/fakes.py`). Pausing the fake `db` breaks the fake
app the same way pausing the real one does. They cover:
- policy validation, including injection attempts, extra fields and limits
- threshold maths (error rate, p95, window, minimum samples)
- the full lifecycle: completion, automatic abort on breach, kill switch (in-process and cross-process), injection failure → rollback, unverifiable removal → BLOCKED → unblock, unrecovered app → BLOCKED
- overlap rejection and the cross-process lock with stale-lease expiry
- crash recovery, including not interfering with another live controller
- idempotent cleanup and emergency-sweep scoping
- the watchdog (acts, stands down, blocks on failure)
- dependency discovery (declared, inferred and manual edges; runtime status)
- the API (rejections, 422/400/423, preflight, kill)
- the planner: valid plans and ranking, malformed JSON, invalid parameters, unauthorized targets, injected fields,
  provider failures, retry, and evidence-driven rules ranking
- evidence analysis for db pause, broker pause and latency; zero baselines, missing samples and inconclusive runs
- the analyst: grounded diagnoses, rejected citations, fallback, inconclusive handling, untrusted logs, retests
- severity and resilience maths, the unrecovered floor and cap, coverage, and weak reruns not replacing results
- sequencing: completion, stop on BLOCKED, rejection and kill switch, and AI failure not affecting rollback
- evidence records, JSON export, the Day 2 API and the findings report

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `docker CLI not found` / `Docker engine not reachable` | Start Docker Desktop and wait for "Engine running"; or set `CHAOS_DOCKER_BIN` to the full path of `docker.exe` |
| `compose up` times out on first start | The Paperless image is large; run `docker compose -p paperless-chaos -f sandbox/docker-compose.yml pull` and then `python -m chaos up` again |
| Preflight: `webserver running, not paused, healthy` fails | Paperless is still migrating on first boot (start period 120 s). Wait, then run `python -m chaos status` |
| Dashboard banner **Experiments blocked** | Run `python -m chaos rollback`, check `docker ps`, then click "Verify environment & unblock" (or `python -m chaos unblock`) |
| A container stays paused after a crash | `python -m chaos rollback` (works without the server); or `docker unpause paperless-chaos-db-1` |
| `network_latency` preflight fails "netem helper image" | `python -m chaos up` rebuilds `chaos-netem:local` (needs Docker Hub access for `alpine:3.20`) |
| Port 8000 or 8800 in use | Change the published port in `sandbox/docker-compose.yml` and set `CHAOS_PAPERLESS_URL`; or `python -m chaos serve --port 8801` |
| Token errors | Delete the Paperless data with `python -m chaos down --volumes`, then `up` (admin is created on first boot from `sandbox/.env`) |

## 9. Known limitations

- One target per experiment; faults only on `db` and `broker`.
- The pause fault freezes the container, but it doesn't kill connections or simulate a refused connection.
- Thresholds use a rolling window over probes that have finished. A probe that hangs for 5 s is counted when it
  times out, so detection takes about the probe timeout plus one tick.
- The watchdog guarantee is best-effort. If Docker itself is down, nothing can unpause the container, and the
  controller will report BLOCKED.
- Metrics come from the synthetic workload only; there is no Prometheus or cAdvisor resource telemetry.
- `docker pause` is not supported for Windows containers. The sandbox needs Linux containers (Docker Desktop default).
- The experiment catalogue has four scenarios (pause and latency on `db` and `broker`). The resilience score covers
  only those and says nothing about faults outside it.
- Root causes are hypotheses drawn from black-box measurements. No Paperless code or configuration is inspected.
- Workflow-to-service expectations in `catalog.py` are assumptions from the architecture, used to rank experiments
  and to flag surprises.
- When thresholds abort a run after a few seconds, each workflow may have a single fault-phase sample. The finding
  lists this, and its confidence is lowered.
- The rules fallback produces template diagnoses chosen by fault type. They are labelled as such and are weaker than
  a model reading the evidence.
- Recommended fixes are not applied or verified automatically. `retest` compares before and after once you have
  applied one.
