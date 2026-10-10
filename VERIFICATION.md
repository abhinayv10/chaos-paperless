# Day 1 verification (2026-10-10, Windows 11 + Docker Desktop 4.94 / engine 29.8.2, WSL2)

These results come from real runs against the live sandbox. Run IDs refer to rows in `data/chaos.sqlite3`.

| # | Day 1 criterion | Implementation | Evidence |
|---|---|---|---|
| 1 | Paperless-ngx runs end-to-end in an isolated sandbox | `sandbox/docker-compose.yml` (Paperless 3.2.1 pinned by digest, postgres:18, valkey:9; 127.0.0.1 only) + `python -m chaos up` | `up` waited for all 3 services to report healthy. `seed`: 2 synthetic docs uploaded and processed (`success`, ~1.1 s each), `count=2`. `status`: all checks PASS |
| 2 | Dependency map of the actual environment | `chaos/discovery.py` (compose + `docker inspect`), Dependency Map tab with manual corrections | `/api/graph`: webserver→db (declared `depends_on service_healthy` + inferred `PAPERLESS_DBHOST`), webserver→broker (declared + inferred `PAPERLESS_REDIS`), host→webserver (inferred, published port); live health per node |
| 3 | Baseline health / error rate / latency under a repeatable workload | `chaos/workload.py` v1, `baseline-20261010-094410-7ea4` | 30 s: 0% errors on all probes; api_documents p50 64 ms / p95 241 ms; web_root p50 36 ms; doc upload→processed p50 1117 ms |
| 4 | A real failure injected, with visible application impact | `PauseFault` (`docker pause`) | `exp-20261010-094550-43fc` (broker, 20 s, ran full duration): api_status 0→100% errors, doc_upload 0→67%, doc processing p95 1.1 s→5.3 s, while api_documents reads kept working. `exp-…0110` (db): every probe 0→100% errors, p95 1.1 s→5.0 s |
| 5 | A threshold breach triggers automatic abort / rollback | `safety.evaluate_thresholds` + `engine._execute` | `exp-20261010-094452-0110`: db pause planned 60 s; `threshold_breach` "error rate 100% > limit 30%; p95 5045ms > 4000ms"; fault removed after **6.3 s** |
| 6 | Fault removed and recovery verified | `engine._remove` (verify via inspect) + `_verify_recovery` | All runs: `fault_removed – verified: state=running paused=False`; recovery 1.4 s (db), 0.3 s (broker), 0.5 s (kill). No BLOCKED state, no active-fault marker afterwards |
| 7 | Dashboard shows real experiment state and observations | `dashboard/` served by `python -m chaos serve` (http://127.0.0.1:8800) | Reads only `/api/*` (runs, samples, events, graph). Shows explicit "unavailable" states when Docker or Paperless is down (also checked before Docker was installed) |
| 8 | Repeatable from documented instructions | `README.md` §2–4 | This sequence: `up` → `seed` → `serve` → baseline → db abort → broker degrade → kill switch |

Additional safety checks on the live stack:
- **Kill switch** `exp-20261010-094655-4ac6`: broker `paused` → `POST /api/kill` → `running` immediately, `rollback_verified: true`; the fault lasted 4.2 s of a planned 90 s.
- **Unauthorized target** `webserver` → rejected (recorded as `rejected`). An extra `command` field → HTTP 422.
- **Watchdog** armed for every injection (event `watchdog_armed`). It stood down because the controller removed the fault first. The takeover path is covered by unit tests.
- **Unit tests:** `pytest -q` → 54 passed.

Not verified live: the `network_latency` fault (implemented, and its preflight requires the netem image) and an actual controller crash mid-fault (covered by unit tests only).

# Day 2 verification (2026-10-10, same machine and sandbox)

These results also come from real runs. No LLM provider was available on this machine (no Ollama, no API key), so
both agents ran in their deterministic `rules` mode. The LLM path is covered by unit tests with a fake provider only.

| # | Day 2 step | Evidence |
|---|---|---|
| 1 | Discovery feeds the planner | `python -m chaos plan`: plan `plan-20261010-172932-4feb`, 4 ranked recommendations built from the live dependency map and 8 earlier runs. db pause ranked first (all 5 workflows pass through db; last run critical), broker latency second (never tested) |
| 2 | Safety validation and sequential execution | `python -m chaos plan-run latest`: 4 experiments in sequence, each with its own preflight (13/13), baseline, watchdog, verified rollback and verified recovery. Sequence `completed` |
| 3 | Controlled faults with observed impact | `exp-…172934-9b10` db pause: aborted by threshold after 6 s, full impact. `exp-…173020-24c7` broker latency 300 ms: api_status 50% errors, document processing p95 1.1 s -> 13.4 s, reads unaffected. `exp-…173118-bd9b` db latency 300 ms: api_documents and api_status 100% errors. `exp-…173219-b1a2` broker pause: api_status and doc_upload failed, reads unaffected |
| 4 | Diagnosis and recommended fix | Every run has a finding citing numbered facts, e.g. broker pause -> hypothesis "requests wait on broker with no effective timeout", fix category `timeout_configuration`, with success criteria |
| 5 | Scoring | `python -m chaos score`: overall resilience 65.7/100, coverage 4/4 scenarios, 12 experiments analysed (4 completed, 8 aborted, 0 inconclusive, 0 unrecovered). Severity: db pause 80 critical, db latency 70 high, broker pause 50 high, broker latency 44 medium |
| 6 | Report, export, API | `python -m chaos report findings` (7 sections), 12 files in `data/exports/`, and `/api/plan`, `/api/findings`, `/api/report` returned 200 from a live `serve`. A second `POST /api/plan` re-ranked on the new evidence (db latency moved from 3rd to 2nd) |
| 7 | Safety preserved | After the sequence: `status` all PASS, not blocked, no active-fault marker. `webserver` target still rejected with 400; `POST /api/kill` returned `rollback_verified: true` |

- **`network_latency` is now verified live** on both db and broker (it was not on Day 1).
- **Unit tests:** `pytest -q` -> 122 passed (54 from Day 1, 68 new).
- **Engine change:** recovery verification now also requires the newest sample of each recovery probe to be good.
  The old rule could verify recovery on consecutive `api_status` successes while `api_documents` was still failing,
  which made `test_unrecovered_application_blocks` fail about half the time.

Not verified: a real LLM provider end to end, the new dashboard tabs in a browser (their API calls and element ids
were checked, the rendering was not looked at), `retest` against the live sandbox, and a sequence stopping on a real
BLOCKED state (unit tests only).
