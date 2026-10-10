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
