# Midterm: Fault-Tolerant University Information System

## Goal
Build a small distributed university platform in two versions (baseline and fault-tolerant),
inject failures, measure detection/recovery, and compare reliability and availability.
All results must come from real runs (CSV/logs), not from assumptions.

## Stack
Python 3.11, FastAPI, httpx (async), PostgreSQL 16, nginx (load balancer),
Docker Compose, Locust (high load), pandas + matplotlib (analysis).
Kubernetes is NOT used. Keep everything runnable with `docker compose` on one laptop.

## Services
- gateway: nginx, single entry point on localhost:8080
- student-service: students, course registration, timetable (with cache)
- payment-service: tuition payments; calls mock-bank
- transcript-service: generates transcripts, calls student-service
- mock-bank: external payment provider simulator with a fault-injection endpoint
  (POST /admin/fault {mode: none|delay|error|crash_after_debit, delay_s})
- postgres-primary, postgres-replica (streaming replication)
- monitor: polls /health of every instance every 0.5 s and logs state changes to CSV

## Two modes
One codebase. Behavior switched by env var FT_MODE=off|on.
- docker-compose.baseline.yml: 1 instance per service, single DB, FT_MODE=off,
  no retries, no timeouts beyond defaults, no circuit breaker, no idempotency.
- docker-compose.ft.yml: 2 instances per service behind nginx, primary+replica DB,
  restart: unless-stopped, FT_MODE=on.

## Hardware / infrastructure fault tolerance (implement all)
1. Service replicas (2 per service) behind nginx with passive health checks
   (max_fails, fail_timeout, proxy_next_upstream).
2. Database replication: primary + streaming replica. If primary is down, reads go to
   replica (degraded read-only mode); a failover script `infra/failover.sh` promotes the replica.
3. Self-healing: Docker restart policies and healthchecks in compose.
4. Backup: scheduled pg_dump into ./backups with a restore script.
5. "Node" = group of containers. Put replica #2 of every service into node-b
   (compose labels) so a node failure can be simulated by stopping that group.
RAID and ECC: document only (docs/hardware_ft.md), no implementation.

## Software fault tolerance (implement all, only active when FT_MODE=on)
1. Timeouts on every outgoing HTTP and DB call.
2. Retry with exponential backoff + jitter (max 3 attempts), only for transient errors.
3. Circuit breaker (closed/open/half-open) on payment -> mock-bank and transcript -> student.
4. Idempotent payments: Idempotency-Key header + UNIQUE constraint; duplicate request
   returns the original result (duplicate-request detection).
5. Payment as a DB transaction with rollback; "pending" record + reconciliation job that
   resolves interrupted payments (crash_after_debit) without double charge.
6. Checkpointing: batch transcript generation saves progress every N students and resumes
   from the last checkpoint after a crash.
7. Health endpoints: /health (liveness) and /ready (DB + dependencies).
8. Graceful degradation: timetable served from cache when DB is down; transcript returns
   a partial/cached result instead of 500 when student-service is down.

## Workload and failure injection
- experiments/workload.py: steady async load (default 20 req/s, mixed endpoints, every
  payment has a unique idempotency key, some keys are deliberately resent).
  Logs every request: timestamp, endpoint, status, latency_ms, attempt info.
- experiments/inject.py: scenarios
  1. app_crash: docker kill one payment-service instance
  2. db_failure: stop postgres-primary for 30 s
  3. network_timeout: mock-bank delay 5 s for 30 s
  4. node_failure: stop all node-b containers
  5. lost_transaction: mock-bank crash_after_debit during payments
  6. high_load: Locust, 200 users, 60 s
- experiments/run_all.py: for each scenario and each mode (baseline, ft):
  start stack, warm up 15 s, inject at t=20 s, run 90 s total, stop, run consistency check.
- experiments/consistency_check.py: no duplicate payments, ledger total equals sum of
  completed payments, no pending payments left after reconciliation, transcripts complete.
- experiments/soak.py: 20 min run in each mode with a random failure every ~2 min
  (used for observed MTTF/MTBF/MTTR).

## Measurements (definitions, use exactly these)
- detection_time_s: injection time -> first unhealthy state in monitor log or breaker open
- recovery_time_s: injection time -> start of first 10 s window with >= 99% success
- failed_requests, successful_requests, recovered_requests (succeeded after >= 1 retry)
- availability = successful / total requests in the run
- consistency: PASS/FAIL with reason
Output: results/experiments.csv (one row per scenario x mode), results/raw/*.csv,
results/soak_*.csv, results/plots/*.png.

## Analysis
analysis/metrics.ipynb or analysis/metrics.py:
- observed MTTF, MTBF, MTTR, failure rate (lambda = 1/MTTF), availability from soak runs
- theoretical availability from an RBD of the architecture (series/parallel, A = MTTF/(MTTF+MTTR))
  and comparison with observed values, with explanation of discrepancies
- before/after table baseline vs ft for every scenario, plus plots (success rate over time
  with the injection moment marked)
- architecture diagram and fault tree as PNG (graphviz or mermaid)

## Rules for working
- Work stage by stage (see stages below). After each stage: run it, show proof it works,
  and stop for my confirmation.
- Keep code simple and readable; I must explain it in an oral defense.
- After each stage append to docs/DEFENSE_NOTES.md: what was built, why, which file and
  function implements each mechanism, and how to demo it. Plain language, step by step.
- Never use em dashes in any text or docs.
- Do not fake results. If something does not work, say so.

## Stages
1. Scaffold + baseline: services, DB schema with seed data, baseline compose, workload smoke test.
2. Hardware FT: replicas, nginx, DB replication + failover script, restart policies, backups, node-b.
3. Software FT: all 8 mechanisms behind FT_MODE.
4. Failure injection + experiments: inject.py, run_all.py, consistency_check.py, soak.py; run everything.
5. Analysis: metrics, theory vs observed, plots, architecture diagram, fault tree.
6. Demo + README: demo.sh with 3 scenarios (app_crash, db_failure, lost_transaction), README with run instructions.