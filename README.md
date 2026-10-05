# Fault-Tolerant University Information System

A small distributed university platform built twice: a **baseline** with no fault tolerance and a
**fault-tolerant (FT)** version. Failures are injected, detection and recovery are measured, and
reliability and availability are compared. All numbers come from real runs (`results/`).

## Stack
Python 3.11, FastAPI, httpx, asyncpg, PostgreSQL 16 (primary + streaming replica), nginx, Docker Compose,
Locust, pandas + matplotlib. One laptop, no Kubernetes.

## Services
| Service | Role |
|---|---|
| gateway (nginx, `localhost:8080`) | single entry point, load balancer |
| student-service | students, registrations, timetable (with cache) |
| transcript-service | transcripts (calls student-service), batch generation with checkpoints |
| payment-service | tuition payments (calls mock-bank), idempotency, reconciliation |
| mock-bank (`localhost:8090`) | fake bank with `POST /admin/fault {mode: none\|delay\|error\|crash_after_debit}` |
| postgres-primary / postgres-replica | DB, streaming replication |
| monitor | polls `/health` and `/ready` of every instance every 0.5 s, logs state changes |
| backup (FT) | `pg_dump` every 60 s into `backups/` |

One codebase (`services/`), behavior switched by `FT_MODE=off|on`.

| | `docker-compose.baseline.yml` | `docker-compose.ft.yml` |
|---|---|---|
| Instances | 1 per service | 2 per service (instance 2 on "node-b") |
| DB | primary only | primary + replica, `infra/failover.sh` |
| Self-healing | none | `restart: unless-stopped`, healthchecks |
| Software FT | none | timeouts, retry + backoff + jitter, circuit breakers, idempotency, transaction + reconciliation, checkpointing, `/ready` with dependencies, graceful degradation |

## Requirements
Docker Desktop (Compose v2) and `python3` with `pandas` and `matplotlib` (only for the analysis).
Everything else runs in containers.

## Run
```bash
# fault-tolerant version
docker compose -f docker-compose.ft.yml up -d --build
curl localhost:8080/timetable/1
curl -X POST localhost:8080/payments -H 'content-type: application/json' \
     -H 'Idempotency-Key: k1' -d '{"student_id":1,"amount":100}'
docker compose -f docker-compose.ft.yml down -v

# baseline version (same commands with docker-compose.baseline.yml)
```
Only one stack can run at a time (both use ports 8080 and 8090).

## Demo (3 scenarios)
```bash
./demo.sh ft all              # app_crash, db_failure, lost_transaction, pauses between steps
./demo.sh baseline all        # same faults on the baseline, for comparison
./demo.sh ft db_failure       # one scenario
DEMO_FAST=1 ./demo.sh ft all  # no pauses
```
Each scenario starts a fresh stack, runs 20 req/s for 45 s, injects the fault at t=10 s and shows what the
monitor saw, the workload result and the consistency check. About 1.5 min per scenario.

## Experiments
```bash
python3 experiments/inject.py <scenario> <baseline|ft>  # one fault on a running stack
python3 experiments/consistency_check.py <baseline|ft>  # money and transcripts correct?
python3 experiments/run_all.py     # 6 scenarios x 2 modes, about 40 min (resumes; --fresh reruns all)
python3 experiments/soak.py        # 20 min per mode, random fault every ~2 min (resumes)
```
Scenarios: `app_crash`, `db_failure`, `network_timeout`, `node_failure`, `lost_transaction`, `high_load`.

## Analysis
```bash
python3 analysis/metrics.py    # tables, MTTF/MTBF/MTTR, RBD vs observed, plots
python3 analysis/diagrams.py   # architecture.png, fault_tree.png
```

## Results (90 s runs, 20 req/s)
| Scenario | Availability baseline | Availability FT | Consistency baseline / FT |
|---|---|---|---|
| app_crash | 86.22% | 99.94% | FAIL / PASS |
| db_failure | 66.33% | 90.44% | FAIL / PASS |
| network_timeout | 95.39% | 92.00% | FAIL / PASS |
| node_failure | 22.33% | 100.00% | FAIL / PASS |
| lost_transaction | 92.78% | 92.78% | FAIL / PASS |
| high_load | 100.00% | 100.00% | FAIL / PASS |

Soak (20 min, same 10 faults): baseline 36.7% availability, FT 97.3% (MTTF 173 s, MTTR 27 s).
RBD theoretical availability: FT 0.972 vs observed 0.973; baseline 0.773 vs observed 0.367.
Explanations, including why FT is lower in `network_timeout`: `docs/DEFENSE_NOTES.md`.

## Where things are
| Path | Content |
|---|---|
| `services/` | all service code; `ft.py` = retry, circuit breaker, timeouts |
| `gateway/` | nginx configs (baseline, FT) |
| `db/init/` | schema, seed data (50 students, 10 courses), replication role |
| `infra/` | `failover.sh`, `backup.sh`, `restore.sh`, replica entrypoint |
| `experiments/` | workload, injection, consistency check, run_all, soak, Locust |
| `analysis/` | metrics and diagrams |
| `results/` | `experiments.csv`, `before_after.md`, `soak_*.csv`, `rbd.csv`, `raw/`, `plots/` |
| `docs/DEFENSE_NOTES.md` | what was built in every stage, which function does what, real results, how to demo |
| `docs/hardware_ft.md` | RAID and ECC (documentation only) |
