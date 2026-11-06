# Defense Notes

## Stage 1: Scaffold and baseline

### What was built
A small university system made of 4 Python (FastAPI) services, one PostgreSQL database
and an nginx gateway. This is the **baseline**: no fault tolerance at all. Later stages
add fault tolerance and compare against this version.

| Part | File | What it does |
|---|---|---|
| Shared code | `services/common.py`, `create_app()` | Creates each app, the DB connection pool, `/health` and `/ready` |
| student-service | `services/student.py` | `GET /students/{id}`, `POST /registrations`, `GET /timetable/{id}` |
| transcript-service | `services/transcript.py`, `transcript()` | Calls student-service over HTTP, reads grades, computes GPA |
| payment-service | `services/payment.py`, `create_payment()` | Saves a `pending` payment, charges mock-bank, marks it `completed`, writes the ledger |
| mock-bank | `services/mockbank.py` | Fake bank. `POST /charge` debits money. `POST /admin/fault` switches modes: `none`, `delay`, `error`, `crash_after_debit` |
| Gateway | `gateway/nginx.baseline.conf` | Single entry point on `localhost:8080`, routes by URL path |
| DB schema + seed | `db/init/01_schema.sql`, `02_seed.sql` | 50 students, 10 courses, 4 registrations and grades per student |
| Stack | `docker-compose.baseline.yml` | 1 instance per service, 1 DB, `FT_MODE=off`, no restart policy |
| Load generator | `experiments/workload.py` | Steady load (default 20 req/s), logs every request to CSV |

### Why it is built this way
- **One codebase, one Docker image.** All services live in `services/` and share one
  Dockerfile. Compose picks the service with the command (`uvicorn student:app`, etc.).
  `FT_MODE` (in `common.py`) will switch fault tolerance on in stage 3.
- **The baseline is weak on purpose.** No retries, no timeouts except the httpx default (5 s),
  no circuit breaker, no idempotency, no transaction around a payment. This gives a fair
  "before" picture.
- **mock-bank remembers every debit** (`GET /charges`) and receives the payment's
  idempotency key as `ref`. Later, reconciliation and the consistency check use this
  to prove there is no double charge.
- **crash_after_debit** means the bank takes the money and then fails before answering.
  The payment service does not know whether the money was taken. This is the
  "lost transaction" case.

### Workload (`experiments/workload.py`)
- Mix: 40% timetable, 20% student lookup, 20% payment, 10% registration, 10% transcript.
- Each payment has a unique `Idempotency-Key`. 10% of payments deliberately resend an old key
  (column `resent=1`) to test duplicate detection.
- CSV columns: `ts, endpoint, status, latency_ms, attempts, idempotency_key, resent`.
  `status=0` means no response at all. `attempts` comes from the `X-Attempts` response header
  (always 1 in the baseline, stage 3 adds retries).

### Proof it works (real run, 2026-10-05)
30 s smoke test at 20 req/s:
```
endpoint        total    ok %   p50 ms   p95 ms
timetable         239   100.0      3.5      5.0
student           117   100.0      3.2      4.6
payment           125   100.0     15.7     19.3
registration       60   100.0      4.1      6.4
transcript         59   100.0     13.5     17.1
ALL               600   100.0      4.1     17.0
```
Baseline weakness, shown with real data: the workload resent 7 keys, and the database
holds **7 duplicate payments** (126 completed payments but only 119 unique keys).
So without idempotency the student is charged twice.

### How to demo
```bash
docker compose -f docker-compose.baseline.yml up -d --build
curl localhost:8080/students/1
curl localhost:8080/timetable/1
curl localhost:8080/transcripts/1
curl -X POST localhost:8080/payments -H 'content-type: application/json' \
     -H 'Idempotency-Key: demo-1' -d '{"student_id":1,"amount":100}'

# make the bank fail, payment returns 502
curl -X POST localhost:8090/admin/fault -H 'content-type: application/json' -d '{"mode":"error"}'
curl -X POST localhost:8080/payments -H 'content-type: application/json' -d '{"student_id":1,"amount":100}'
curl -X POST localhost:8090/admin/fault -H 'content-type: application/json' -d '{"mode":"none"}'

# load test, then count duplicate payments
docker compose -f docker-compose.baseline.yml run --rm workload \
     python /exp/workload.py --duration 30 --out /results/raw/smoke_baseline.csv
docker compose -f docker-compose.baseline.yml exec postgres-primary psql -U uni -d uni -c \
  "SELECT count(*) - count(DISTINCT idempotency_key) AS duplicates FROM payments WHERE status='completed';"

docker compose -f docker-compose.baseline.yml down -v   # stop and wipe the DB
```

## Stage 2: Hardware / infrastructure fault tolerance

### What was built
The file `docker-compose.ft.yml` runs the same code with redundancy. Nothing in the service
code depends on how many copies run; all redundancy lives in compose, nginx and Postgres.

| Mechanism | Where | How it works |
|---|---|---|
| 1. Service replicas | `docker-compose.ft.yml` (`student-service-1/2`, `payment-service-1/2`, `transcript-service-1/2`) | Two copies of every service |
| 1. Load balancer with passive health checks | `gateway/nginx.ft.conf` | Round robin. `max_fails=1 fail_timeout=5s`: after one failed request an instance is skipped for 5 s. `proxy_next_upstream error timeout http_502`: the request is retried on the other instance. POSTs already sent are not retried (that could charge twice). `resolve` + Docker DNS: a restarted container is found again even with a new IP |
| 2. DB replication | `db/init/00_replication.sh`, `infra/replica-entrypoint.sh` | Primary creates a `replicator` role. Replica copies the primary with `pg_basebackup -R` and then streams every change (async streaming replication) |
| 2. Read-only degraded mode | `services/common.py`, `db()` | Every query goes to the primary first. On a connection error the same query goes to the replica. Reads work. Writes fail on the replica with `ReadOnlySQLTransactionError`, which `create_app()` turns into HTTP 503 "database is read-only" |
| 2. Failover | `infra/failover.sh` | Runs `SELECT pg_promote()` on the replica. Since services already fall back to the replica, writes work again without restarting any service |
| 3. Self-healing | `docker-compose.ft.yml` (`x-app`) | `restart: unless-stopped` restarts a crashed container. `healthcheck` calls `/health` every 2 s and marks it unhealthy. `init: true` makes tini PID 1 (see note below) |
| 4. Backups | `infra/backup.sh` (container `backup`), `infra/restore.sh` | `pg_dump` every 60 s into `./backups`, keeps the newest 10. Uses the replica if the primary is down. `restore.sh` loads the newest (or a given) dump into the primary with `pg_restore --clean` |
| 5. Node-b | labels `node: a` / `node: b` | Replica #2 of each service and `postgres-replica` are on node-b. Node failure = `docker stop $(docker ps -q --filter label=node=b)` |
| Monitor | `services/monitor.py` (in both compose files) | Polls `/health` and `/ready` of every instance every 0.5 s. Writes only state changes to `results/monitor_<mode>.csv`. States: `up`, `degraded` (alive but DB on replica or not ready), `down` |
| RAID, ECC | `docs/hardware_ft.md` | Documentation only |

### Important findings (real, not assumed)
- **`docker kill` does not trigger the restart policy.** Docker treats it as a manual stop. Our first
  test: container stayed `exited`, `RestartCount=0`. A real crash is simulated by killing the
  process inside the container: `docker exec <container> sh -c "kill -9 -1"`. For this to work
  the app must not be PID 1, so FT services use `init: true`.
- **Backups lose recent data.** Restore brought back 388 of 427 payments: the 39 made after the last
  dump were lost. Recovery point objective (RPO) = backup interval (60 s). The replica has
  almost no data loss, but it also copies mistakes (`TRUNCATE` reached it within 1 s).
- **Async replication** means a write confirmed just before the primary dies may not have reached
  the replica. It is the Postgres default and fine for this project.
- **After failover, do not start the old primary again.** Services try `postgres-primary` first,
  so two primaries would split the data. Reset with `docker compose -f docker-compose.ft.yml down -v`.

### Proof it works (real runs, 2026-10-05, workload 20 req/s)
| Test | What happened | Result |
|---|---|---|
| Replication | `pg_stat_replication` on primary: `streaming, async`. Payment written through the gateway is readable on the replica | OK |
| Load balancing | 30 timetable calls: 15 on `student-service-1`, 15 on `student-service-2` | OK |
| App crash (`docker kill payment-service-1`, before `init: true`) | Not restarted. 3 of 800 requests failed (504 after 1 s connect timeout), others went to payment-2 | 99.6% ok |
| App crash (`kill -9` inside container) | Monitor: down at +0.5 s, up at +1 s. Docker: `restarts=1`, healthy within 4 s | 800/800 ok (100%) |
| Node failure (stop 4 node-b containers for 20 s) | Monitor: student-2, transcript-2, payment-2 down at once, up again after restart | 800/800 ok (100%) |
| DB primary stopped | Monitor: all 6 services `degraded, db on replica` in the same poll. Reads 208/208 ok, writes 2/92 ok (90 got 503 read-only) | Degraded mode works |
| `failover.sh` 15 s later | Replica promoted (`pg_is_in_recovery = f`), first successful write +1.3 s later. Writes 141/146 ok after failover | OK |
| Backup + restore | `TRUNCATE ledger, payments, grades`, then `infra/restore.sh` | 200/200 grades back, 388/427 payments (RPO 60 s) |

Raw CSVs: `results/raw/s2_*.csv`, monitor log: `results/monitor_ft.csv`.

### How to demo
```bash
docker compose -f docker-compose.ft.yml up -d --build
docker compose -f docker-compose.ft.yml ps            # all healthy, node labels visible

# replication
docker compose -f docker-compose.ft.yml exec postgres-primary psql -U uni -d uni \
  -c "SELECT client_addr, state FROM pg_stat_replication;"

# self-healing: crash the process, watch it come back
docker exec uni-ft-payment-service-1-1 sh -c "kill -9 -1"
docker inspect -f '{{.State.Status}} restarts={{.RestartCount}}' uni-ft-payment-service-1-1
tail results/monitor_ft.csv

# node failure while load runs (second terminal)
docker compose -f docker-compose.ft.yml run --rm workload python /exp/workload.py --duration 40
docker stop $(docker ps -q --filter label=node=b)
docker start $(docker ps -aq --filter label=node=b)

# DB failure: reads ok, writes 503, then failover
docker stop uni-ft-postgres-primary-1
curl localhost:8080/timetable/1                       # works (replica)
curl -X POST localhost:8080/registrations -H 'content-type: application/json' \
     -d '{"student_id":1,"course_id":2}'               # 503 read-only
./infra/failover.sh
curl -X POST localhost:8080/registrations -H 'content-type: application/json' \
     -d '{"student_id":1,"course_id":2}'               # 201

# backup / restore (needs a fresh stack: down -v, up)
ls backups
./infra/restore.sh
```

## Stage 3: Software fault tolerance

### Where it lives
- `services/ft.py`: the reusable parts: timeouts (constants), `retry()`, `CircuitBreaker`.
- `services/common.py`, `services/payment.py`, `services/transcript.py`, `services/student.py`:
  where each mechanism is used.
- Everything checks `common.FT_MODE`. With `FT_MODE=off` (baseline) the old behavior runs.
- Every response has headers `X-Attempts` (1 + retries done) and `X-Instance` (which container
  answered). The workload logs `X-Attempts`, so recovered requests can be counted.

### The 8 mechanisms
| # | Mechanism | Code | How it works, in plain words |
|---|---|---|---|
| 1 | Timeouts | `ft.HTTP_TIMEOUT_S`, `ft.DB_TIMEOUT_S`, used in `common.http_client()` and `common.get_pool()` | Every HTTP call gives up after 2 s, every DB connect and query after 2 s. A slow bank cannot hold a request for long |
| 2 | Retry, backoff, jitter | `ft.retry()`, used in `common.db()`, `payment.create_payment()`, `transcript.fetch_student()` | Up to 3 attempts. Waits 0.1 s, then 0.2 s, plus a random extra (jitter) so many clients do not retry at the same moment. Retries only transient errors: `common.CONN_ERRORS` for the DB, `ft.is_transient_http()` (network error, 502/503/504) for student-service, and `payment.bank_did_not_debit()` for the bank |
| 3 | Circuit breaker | `ft.CircuitBreaker`, objects `payment.bank_breaker`, `transcript.student_breaker` | closed: calls pass. 5 failed calls in a row: open, calls fail at once (5 ms instead of 0.5 s) without touching the sick service. After 10 s: half_open, one trial call. Success: closed. Failure: open again. Each instance has its own breaker. State is shown in `/ready` and printed to the container log |
| 4 | Idempotent payments | `payment.create_payment()`, `payment.replay()`, index created in `payment.ft_startup()` | Client sends `Idempotency-Key`. Same key again: the stored result is returned (`"replayed": true`), nothing new is charged. `UNIQUE` index on `idempotency_key` + `INSERT ... ON CONFLICT DO NOTHING` also catches two copies arriving at the same moment |
| 5 | Transaction + reconciliation | `payment.complete()`, `payment.reconcile()`, `payment.reconcile_loop()`, `POST /admin/reconcile` | Completion (`status=completed` + ledger row) is ONE transaction: both or none. If the bank answer is unknown (timeout, `crash_after_debit`), the payment stays `pending` and the client gets 202. Every 5 s, payments pending longer than 30 s are checked at the bank by key: charge found: completed, not found: failed. `WHERE status='pending'` makes sure only one instance writes the ledger |
| 6 | Checkpointing | `transcript.run_job()`, `transcript.save()`, `transcript.resume_loop()`, table `transcript_jobs` | `POST /transcripts/batch` builds transcripts for all 50 students. Every 10 students the results and `last_student_id` are saved in one transaction. If the runner dies, after 15 s without a checkpoint another instance takes the job (`FOR UPDATE SKIP LOCKED`, only one wins) and continues after `last_student_id` |
| 7 | Health endpoints | `common.create_app()`: `/health`, `/ready` | `/health` = process alive. `/ready` = DB reachable (primary or replica), dependencies up (payment: mock-bank, transcript: student-service), breakers closed. Otherwise 503 with a `reason`, which the monitor logs |
| 8 | Graceful degradation | `student.timetable()`, `transcript.transcript()` | DB down: timetable from the in-memory cache (`"source": "cache"`). Student-service down: transcript from cache, or grades only, with `"partial": true`, status 200 instead of 500 |

### Design decisions to be able to defend
- **Why not retry every bank error?** A timeout or a 500 can happen after the bank took the money
  (`crash_after_debit`). Retrying would charge twice. We retry only when the money surely did not
  move (connection refused, 503). Unknown outcomes go to reconciliation instead.
- **Why wait 30 s before reconciling?** The bank may still be processing a slow request. In our
  test the bank debited `slow-1` 5 s after the client gave up. Checking too early would mark it
  failed while the money was taken.
- **Retry inside the breaker.** `bank_breaker.call(lambda: ft.retry(...))`: one breaker failure =
  one call that failed after all retries.
- **202 is counted as success** by the workload (2xx). The money is correct in the end, which the
  consistency check (stage 4) proves.
- **Limits.** Caches are per instance and in memory (lost on restart, may be stale after a
  registration on the other instance). Breaker state is per instance.

### Proof it works (real runs, 2026-10-05)
| Test | FT result | Baseline result |
|---|---|---|
| Same key sent twice | 2nd answer 200 `replayed: true`, 1 DB row, 1 bank charge. Direct duplicate INSERT rejected by the UNIQUE index | 4 duplicate payments in a 20 s run |
| 30 s workload with 9 resent keys | 600/600 ok, 0 duplicates, ledger rows = completed payments (110) | |
| Bank `error` | Each request `attempts=3`, about 0.5 s. After 5 failures per instance the breaker opens: 503 in 5 ms. `/ready` 503 "breaker mock-bank open", monitor `degraded`. Bank fixed: after 10 s half_open, then closed, payments 201 again | 502, `attempts=1` |
| Bank `delay 5 s` | 202 pending after 2.0 s (timeout), not retried. Reconciled to completed, 1 ledger row, 1 bank charge | |
| Bank `crash_after_debit` | 202 pending. Same key again: 202 replayed. Reconciled to completed, 1 ledger row, 1 bank charge | **Inconsistent:** payment `failed` in DB, but the bank took the money |
| Batch crash after student 20 (`kill -9`) | Instance 2 logged "runner died, resuming from checkpoint", "starting after student 20". Job done, 50 transcripts | No checkpoints: a crash loses all work and the job stays `running` |
| Both DBs stopped | Cached timetable 200 (`source: cache`). Not cached: 503 | |
| Both student-service instances stopped | Transcripts 200 `partial: true` (cache or grades only). Back to `live` after restart | 500 |

### How to demo
```bash
docker compose -f docker-compose.ft.yml up -d --build
pay() { curl -s -X POST localhost:8080/payments -H 'content-type: application/json' \
        -H "Idempotency-Key: $1" -d '{"student_id":1,"amount":100}'; echo; }
bank() { curl -s -X POST localhost:8090/admin/fault -H 'content-type: application/json' -d "$1"; echo; }

pay k1; pay k1                                   # 2nd is replayed, no new charge

bank '{"mode":"error"}'
for i in $(seq 12); do curl -s -o /dev/null -w "%{http_code} attempts=%header{x-attempts}\n" \
  -X POST localhost:8080/payments -H 'content-type: application/json' -d '{"student_id":1,"amount":100}'; done
docker compose -f docker-compose.ft.yml logs payment-service-1 | grep breaker
bank '{"mode":"none"}'

bank '{"mode":"crash_after_debit"}'; pay lost1; bank '{"mode":"none"}'   # 202 pending
docker compose -f docker-compose.ft.yml exec payment-service-1 python -c \
  "import httpx; print(httpx.post('http://localhost:8000/admin/reconcile').text)"   # or wait ~35 s
pay lost1                                                               # now completed

curl -s -X POST localhost:8080/transcripts/batch     # note "instance"
docker exec <that container> sh -c "kill -9 -1"      # after ~5 s
curl -s localhost:8080/transcripts/batch/1           # done, 50 transcripts after ~25 s
```

## Stage 4: Failure injection and experiments

### Files
| File | What it does |
|---|---|
| `experiments/inject.py` | The 6 scenarios. Each fault lasts 30 s, then is reverted (DB / node started again, bank back to `none`). `python3 experiments/inject.py <scenario> <baseline\|ft>` |
| `experiments/run_all.py` | For each scenario and mode: fresh stack, workload 90 s, transcript batch at t=15 s (so checkpointing is tested under the fault), inject at t=20 s, consistency check, metrics, row in `results/experiments.csv`. Skips runs already in the file (resume), `--fresh` reruns all |
| `experiments/consistency_check.py` | No payment completed twice, ledger total = completed payments, no pending payments after reconciliation, no key charged twice by the bank, no bank charge without a completed payment, last transcript job done with all students |
| `experiments/measure.py` | The metric definitions from CLAUDE.md (detection, recovery, counts, availability) |
| `experiments/soak.py` | 20 min per mode, a random fault every 90 to 150 s. Fixed seed (42): both modes get the same faults at the same times |
| `experiments/locustfile.py` | High load: 200 users |

How the scenarios inject faults:
- `app_crash`: `kill -9` of the app process inside payment-service(-1). Not `docker kill`: Docker treats it as a manual stop and ignores the restart policy (stage 2 finding). Baseline also has `init: true` so the crash is the same; it has no restart policy.
- `db_failure`: `docker stop postgres-primary`, start after 30 s. No failover in this scenario.
- `network_timeout`: mock-bank `delay 5 s` for 30 s.
- `node_failure`: stop every container labeled `node=b`, start after 30 s. Baseline has no second node, so it is one server: student, transcript, payment and the DB carry `node=b`.
- `lost_transaction`: mock-bank `crash_after_debit` for 30 s.
- `high_load`: Locust, 200 users, 60 s, on top of the normal workload.

Clock check: container clocks and the Mac clock agree within the measurement noise (container time always fell between two host readings), so injection time (host) and log times (containers) can be compared.

### Results (`results/experiments.csv`, 90 s runs, 20 req/s)
| Scenario | Availability baseline | Availability FT | Detection B / FT (s) | Recovery B / FT (s) | Consistency B / FT |
|---|---|---|---|---|---|
| app_crash | 86.22% | 99.94% | 0.20 / 0.24 | never / 0.0 | FAIL / PASS |
| db_failure | 66.33% | 90.44% | 0.10 / 0.15 | 30.5 / 29.5 | FAIL / PASS |
| network_timeout | 95.39% | 92.00% | not detected / 3.71 | 28.5 / 41.0 | FAIL / PASS |
| node_failure | 22.33% | 100.00% | 0.16 / 0.23 | never / 0.0 | FAIL / PASS |
| lost_transaction | 92.78% | 92.78% | not detected / 2.16 | 28.5 / 33.0 | FAIL / PASS |
| high_load | 100.00% | 100.00% | not detected / not detected | 0.0 / 0.0 | FAIL / PASS |

Baseline consistency failures (real numbers from the runs):
- every run: payments completed twice (7 to 33), because resent idempotency keys are charged again.
- `network_timeout`: 75 bank charges without a completed payment (the client gave up after 5 s, the bank debited at 5 s).
- `lost_transaction`: 105 bank charges without a completed payment, 41 keys charged twice.
- `db_failure`, `node_failure`: transcript job stuck at `running`, 0/50 transcripts (no checkpoint, no resume).
FT: all 6 runs PASS.

### What each result means (for the defense)
- **app_crash:** FT restarts the process in about 1 s and nginx sends traffic to payment-2 meanwhile: 1 failed request. Baseline payment-service stays dead for the rest of the run.
- **db_failure:** FT reads continue on the replica (timetable, student, transcript all 100%), only writes fail (112 payments, 60 registrations got 503 read-only) until the primary is back at +30 s. Baseline: every endpoint fails for 30 s. Recovery time is about 30 s in both because writes need the primary (no failover in this scenario).
- **network_timeout: FT has lower availability than baseline (92.0% vs 95.4%).** This is a trade-off, not a bug. FT times out the bank call after 2 s; after 5 failures the breaker opens and rejects payments at once (136 x 503). Baseline waits up to 5 s, so some payments succeed, but **75 times the bank took money for a payment that the system marked as failed**. FT gives up availability to keep money correct.
- **node_failure:** FT 100%: node-a instances and the primary carry everything. **Baseline never recovers, even after the node is back.** Verified: Docker gives the restarted containers new IP addresses (in a repeat test transcript-service got the old IP of the DB and the other way round). The baseline nginx resolves names only at startup, so it keeps sending requests to old IPs, i.e. to the wrong containers (404 and connection errors). The FT nginx uses `resolve` with Docker DNS and finds the new addresses.
- **lost_transaction:** Same availability by coincidence (130 failures each). FT: the breaker opens on the bank's 500s, unknown payments stay pending and are reconciled, money PASS. Baseline: 105 debits without a completed payment.
- **high_load:** 200 Locust users (about 195 req/s, 11 500 requests, 0 failures) did not stress the system in either mode: p95 stayed around 15 ms. Honest result: this load is too small for this laptop to show a difference.
- **Retries rarely show up** (`recovered` is about 0 in the 90 s runs, 6 in the FT soak). Short outages are hidden by nginx retrying on the other instance, which the workload cannot see in `X-Attempts`; long outages are not fixed by 3 quick retries.
- **Detection:** the monitor sees crashes and DB loss within 0.1 to 0.25 s (polling every 0.5 s). Bank problems are only detected in FT (2 to 4 s, via the breaker in `/ready`); the baseline `/ready` checks only the DB, so it never notices.

### Soak (20 min per mode, same 10 faults in both)
Faults: app_crash x3, lost_transaction x4, node_failure x2, db_failure x1.

| Mode | Requests | Availability | Consistency |
|---|---|---|---|
| baseline | 24 000 | 36.72% | FAIL: 30 payments completed twice |
| ft | 24 000 | 97.28% | PASS |

Files: `results/soak_<mode>_requests.csv`, `_events.csv`, `_monitor.csv`, `results/soak_summary.csv`, log `results/run_log.txt`.

### How to demo
```bash
docker compose -f docker-compose.ft.yml up -d
python3 experiments/inject.py app_crash ft          # in another terminal: tail -f results/monitor_ft.csv
python3 experiments/consistency_check.py ft
# full set (about 40 min) and soak (about 42 min):
python3 experiments/run_all.py --fresh
python3 experiments/soak.py
```

## Stage 5: Analysis

Run: `python3 analysis/metrics.py` (tables + plots) and `python3 analysis/diagrams.py` (architecture, fault tree).

### Outputs
| File | Content |
|---|---|
| `results/before_after.md` / `.csv` | Baseline vs FT per scenario (table in stage 4 above) |
| `results/plots/<scenario>.png` | Success rate over time (1 s bins), both modes, injection marked, fault window shaded |
| `results/plots/availability.png` | Availability per scenario, bar chart |
| `results/plots/soak_baseline.png`, `soak_ft.png` | Success rate over 20 min with every injected fault marked |
| `results/soak_metrics.csv` | Observed MTTF, MTBF, MTTR, failure rate, availability |
| `results/rbd.csv` | Component availabilities, RBD per request type, theoretical vs observed |
| `results/plots/architecture.png`, `fault_tree.png` | Diagrams |

### Observed MTTF, MTBF, MTTR (`analysis/metrics.py`, `soak_metrics()`)
Definitions:
- **Component failure** = an injected fault. 10 in 20 min in both modes: component MTBF = 120 s.
- **System failure** = a fault after which users saw errors, i.e. recovery time > 0 (same recovery definition as stage 4: first 10 s window with >= 99% success). A fault that hits a system that is already down is not a new failure: overlapping down intervals are merged.
- MTTR = mean down time per system failure. MTTF = up time / number of failures. MTBF = MTTF + MTTR. Failure rate lambda = 1 / MTTF. Time availability A = MTTF / (MTTF + MTTR).

| Mode | System failures (of 10 faults) | MTTF | MTTR | MTBF | lambda | A (time) | A (requests) |
|---|---|---|---|---|---|---|---|
| baseline | 1, never recovered | 98.4 s | >= 1101.6 s (censored at the end of the run) | 1200 s | 36.6 / h | 0.082 | 0.367 |
| ft | 6, all recovered | 173.0 s | 27.0 s | 200.0 s | 20.8 / h | 0.865 | 0.973 |

How to read it:
- Baseline: the first app_crash (t = 98 s) killed payments for good. Twice the node_failure restarted the dead payment container (monitor: up at 593 s and 1131 s), but after a node restart the gateway points to old IPs, so users never got the system back. One failure that lasted the rest of the run.
- FT: 4 of 10 faults were fully masked (all 3 app_crash, 1 of 2 node_failure). The 6 visible failures, with their recovery times: db_failure 30.5 s, the 4 lost_transaction faults 32.0 to 33.5 s (30 s fault plus the breaker's 10 s open period, partly overlapping), and the other node_failure 1.0 s (a short dip before nginx marked the node-b instances down). Mean = 27.0 s. Writes need the primary and payments need the bank, so these faults cannot be hidden; FT only keeps them from spreading to reads and from corrupting money.
- **A (time) is lower than A (requests)**, because the time definition is strict: one 10 s window with less than 99% success counts as fully down, even if only payments fail (20% of traffic) and reads are 100%.

### Theoretical availability from an RBD (`analysis/metrics.py`, `rbd()`)
Component availability A = 1 - down time / run time, from the soak injection schedule (reverted faults: 30 s; crashed process: until the monitor saw it up again; overlapping down time counted once):

| Component | Faults | A baseline | A FT |
|---|---|---|---|
| node-b (whole node) | 2 | 0.950 | 0.950 |
| payment process (crash) | 3 | 0.190 (no restart) | 0.997 (restart in about 1 s) |
| DB primary | 1 | 0.975 | 0.975 |
| bank | 4 | 0.900 | 0.900 |

Blocks: `series(a, b) = a * b`, `parallel(a, b) = 1 - (1 - a)(1 - b)`.
- **Baseline:** everything in series. Reads: node x DB. Payment: node x payment process x DB x bank.
- **FT:** instance 1 (node-a, not faulted) parallel to instance 2 (node-b). Payment: parallel(payment-1, payment-2 on node-b) x DB primary (writes need the primary) x bank. Reads: parallel(primary, replica on node-b).
- System A = sum over request types of (share in the workload x A of its path): 40% timetable, 20% student, 20% payment, 10% registration, 10% transcript.

| | Baseline | FT |
|---|---|---|
| A path reads (timetable, student, transcript) | 0.926 | 0.999 |
| A path registration | 0.926 | 0.975 |
| A path payment | 0.158 | 0.877 |
| **A theoretical** | **0.773** | **0.972** |
| A observed (requests) | 0.367 | 0.973 |
| A observed (time) | 0.082 | 0.865 |

### Why theory and observation differ
- **FT matches well (0.972 vs 0.973).** Small differences cancel out: the RBD treats the bank as a hard series block, but in reality some payments during bank faults still succeed (202 pending, replayed duplicates return 200), which pushes observed A up; on the other hand the breaker keeps rejecting payments for up to 10 s after the bank is healthy again, and the first requests to a dead instance fail before nginx marks it down, which pushes observed A down.
- **Baseline is far off (0.773 vs 0.367).** The RBD assumes the gateway never fails and that a component is repaired when it is restarted. Both are wrong for the baseline: after a node restart the gateway keeps old IP addresses, a hidden failure of a block we assumed perfect. Lesson: an RBD is only as good as its list of failure modes.
- **Independence assumption.** The RBD multiplies availabilities as if components fail independently. node-b is a common cause: it takes down student-2, transcript-2, payment-2 and the replica at once. With node-a never faulted in our tests this does not hurt, but if node-a also failed, all AND gates would fail together (see the fault tree).
- **Degradation is invisible to the RBD.** Cached timetables and partial transcripts make a block "down" for the model while users still get 200.
- **Time vs request availability.** The RBD gives a probability that a request path works (comparable with request availability), not the strict 10 s window definition, which is why A (time) is lower than both.

### Diagrams
- `results/plots/architecture.png`: FT architecture with node-a / node-b, gateway, replicas, primary + replica, bank, monitor, backup.
- `results/plots/fault_tree.png`: top event "user request fails" = OR(gateway down, AND(both student instances down), AND(both payment instances down), AND(primary and replica down), OR(primary down without failover, bank error / breaker open)). The note on the chart explains masking and the node-b common cause.

## Stage 6: Demo and README

### What was built
- `demo.sh [ft|baseline] [app_crash|db_failure|lost_transaction|all]`: for each scenario a fresh stack,
  20 req/s for 45 s, fault at t=10 s, a live check during the fault (container restarts / read vs write /
  a payment), the monitor's state changes, the workload summary, payment states (lost_transaction) and the
  consistency check. Pauses between steps for talking; `DEMO_FAST=1` removes them.
- `README.md`: what the project is, how to run both versions, demo, experiments, analysis, results, file map.

### Fix found while testing the demo
In a `demo.sh baseline all` run the baseline monitor logged nothing during db_failure. Run alone, the same
scenario was detected after 0.5 s, so the monitor process had most likely died: it caught only network
errors, so a non-JSON answer from `/ready` could crash it, and the baseline monitor had no restart policy.
Fixed in `services/monitor.py` (`check()` catches every error and reports it as a state) and the baseline
monitor now has `restart: unless-stopped` (it is a measurement tool, not part of the system under test).
After the fix `demo.sh baseline all` detects db_failure on the second stack (+0.40 s).
The stage 4 results stand: where the baseline detected something, the monitor was alive at least until
then; the "not detected" baseline cases are bank faults, which the baseline `/ready` does not check, and
high_load, where nothing failed. (The original monitor log cannot be checked any more, the stack was removed.)

### Verified demo output (2026-10-05)
| Scenario | Baseline | FT |
|---|---|---|
| app_crash | container `exited, restarts=0`, payments 20% ok, overall 83%; consistency FAIL incl. `ledger total 3900 != completed payments 4000` (process killed between status update and ledger insert, no transaction) | `restarts=1`, monitor down +0.11 s, up +0.61 s, 900/900 ok, PASS |
| db_failure | read 503, write 503, about 30% ok, monitor `db down` +0.40 s, FAIL | read 200 (replica), write 503, monitor `db on replica` +0.31 s, back +31 s, 78.7% ok, PASS |
| lost_transaction | payment 502, 113 bank charges without a completed payment, FAIL | breaker open after 1.7 s, 5 payments pending then reconciled, PASS |

### How to demo at the defense
```bash
./demo.sh ft all          # about 5 min, press Enter between steps
./demo.sh baseline app_crash
```
Talking points, in order: crash is healed in 1 s and hidden by the second instance; DB loss only blocks
writes; the bank losing answers never loses money in FT (pending + reconciliation), while the baseline
keeps the money and fails the payment.

# Endterm

## Stage E1: Observability

### What was built, and why
Observability lets us see what the system is doing while it runs. It is a measurement tool, not a resilience mechanism, so it runs in both modes and both can be measured the same way.

| Part | File | How it works |
|---|---|---|
| Metrics, logs, correlation ID code | `services/obs.py` | All observability code is in one file |
| Applied to every request | `services/common.py`, `create_app()`, middleware `observe()` | Reads the request ID, times the request, then calls `obs.record_request()` and sets the response headers. Also serves `GET /metrics` |
| Breaker gauge | `services/ft.py`, `CircuitBreaker._set()` | Updates `circuit_breaker_state` on every state change: 0 closed, 1 half_open, 2 open |
| Payment counter | `services/payment.py` | `payments_total{status}` counts events: `completed`, `pending`, `failed`, `replayed`. A reconciled payment counts once as pending and once as completed. `completed` is counted after the commit, so a rollback is not counted |
| Forwarding the ID | `common.http_client()`, `obs.outgoing_headers()` | Every outgoing httpx call carries `X-Request-ID` |
| Gateway | `gateway/observability.conf`, both nginx configs | nginx keeps a valid client ID (letters, digits, `_`, `-`, at most 64 characters) and otherwise uses its own `$request_id`. It logs each request as JSON and returns the ID on every response, including errors it generates itself |
| Prometheus | `prometheus/prometheus.baseline.yml`, `prometheus.ft.yml` | Scrapes every 2 s, evaluates every 5 s. Port 9090, localhost only |
| Grafana | `grafana/provisioning/`, `grafana/dashboards/university.json` | Datasource and the 8-panel dashboard load automatically. Anonymous viewers are allowed. Port 3000, localhost only |
| Trace script | `experiments/trace_request.sh` | Sends one transcript request and fails unless its ID is in the gateway, transcript and student logs |
| Check script | `experiments/check_observability.py` | Checks targets up, every service log line is valid JSON, Grafana loads anonymously, every dashboard query is valid |

### Decisions to be able to defend
- **`endpoint` is the route template** (`/students/{sid}`), not the real path. One label per student would create many time series.
- **`honor_labels: true`.** Prometheus adds its own `instance` label. Without this option it would rename ours to `exported_instance`.
- **`hostname:` in compose.** The `instance` label reads `payment-service-1` instead of a container hash.
- **`/health`, `/ready` and `/metrics` are counted but not logged,** and the dashboard filters them out. The monitor, Docker and Prometheus call them all the time, so they would hide real traffic.
- **`/metrics` is not routed through the gateway** (returns 404). Prometheus scrapes instances directly on the internal network.

### Proof (real runs, 2026-11-04)
| Check | FT | Baseline |
|---|---|---|
| Prometheus targets up | 7/7 | 4/4 |
| Invalid service log lines | 0 | 0 |
| `trace_request.sh` | pass | pass |
| 30 s workload | 600/600 ok, p50 3.8 ms, p95 16.9 ms | 600/600 ok, p50 4.6 ms, p95 17.3 ms |
| Payments: Prometheus = CSV = DB | 110 = 110 = 110 | 126 = 126 = 126 |
| Replayed = resent keys | 9 = 9 | no idempotency |
| Breaker gauge during a bank `error` fault | 0 -> 2 -> 0 on both instances | not used, stays 0 |
| Unhandled error (student-service down) | not applicable, FT returns a partial result | 500 counted, JSON log with traceback, ID in the response |
| Stopped service | | `up` = 0 |

Midterm regression, `demo.sh` on the new code (`results/endterm/e1_regression_demo.txt`):
| Scenario | Result |
|---|---|
| FT app_crash | 900/900 ok, `restarts=1`, PASS |
| FT db_failure | 80.7% ok, PASS |
| FT lost_transaction | 89.6% ok, PASS |
| Baseline app_crash | 84.0% ok, `restarts=0`, FAIL (expected) |

Does observability slow the system down? `db_failure` in FT, 3 runs each, on the same machine on the same day (`results/endterm/e1_db_failure_midterm_vs_e1.jsonl`):
| Code | p50 (ms) | p95 (ms) | Success % |
|---|---|---|---|
| Midterm | 5.8, 6.0, 5.9 | 17.0, 16.7, 16.8 | 81.6, 79.3, 79.2 |
| With E1 | 5.8, 5.2, 6.1 | 16.9, 13.7, 16.7 | 78.8, 80.8, 80.8 |

The ranges overlap, so we see no measurable overhead. Three runs of one scenario cannot prove there is none.

### Known limits (say these before you are asked)
- **Unexplained stall.** In 1 of 4 E1 `db_failure` runs, every request was slow for about 3 s right after the primary stopped (61 requests over 200 ms, p95 945 ms in the demo output). The next 3 E1 runs and the 3 midterm runs did not repeat it, apart from at most one slow request per run. Raw data: `results/endterm/e1_regression/demo_ft_185441_db_failure.csv`. The cause is unknown. Repeated runs in E5 will show how often it happens.
- **A crash can be shorter than the monitor's poll.** With the restart policy, `kill -9` caused an outage of only 0.29, 0.42 and 0.61 s (measured with a 20 ms poll). The monitor polls every 0.5 s and caught 3 of those 3, but missed the crash in the demo run, so detection time can be empty.
- **`/ready` adds traffic in FT.** The transcript `/ready` check calls `/students/1` through the gateway on every monitor poll, so student-service shows extra requests that are not user traffic (about 256 of 439 in one short test). This is midterm behavior that the metrics made visible.
- **`half_open` is rarely visible in the gauge.** It lasts milliseconds and Prometheus scrapes every 2 s. The log lines (`breaker_state_change`) do record it.
- **Prometheus does not scrape nginx or Postgres.** "Instances up" covers service instances only.
- **Not every line is JSON.** The nginx error log and the monitor's own output stay plain text. The gateway log uses `latency_s` instead of `latency_ms`, because nginx only reports seconds.
- **Empty until later stages.** `auth_failures_total` and the 401/403 series until E3, firing alerts until E2. `rate_limited_total` needs a design decision in E3, because nginx sends 429 itself and services never see those requests.
- **Grafana admin password** is still the default, on localhost only. It moves to `.env` in E3.
- **`down -v` deletes Prometheus data.** Any export must happen before the stack stops.
- **Midterm results were measured without observability.** See the overhead table above.

### How to demo
```bash
docker compose -f docker-compose.ft.yml up -d --build
open http://localhost:3000                       # dashboard, no login
open http://localhost:9090/targets               # all targets up

docker compose -f docker-compose.ft.yml run --rm workload python /exp/workload.py --duration 120 &
experiments/trace_request.sh 1                   # one ID through gateway, transcript, student
python3 experiments/check_observability.py ft

# watch the breaker panel go to 2 and back (needs the workload running)
curl -X POST localhost:8090/admin/fault -H 'content-type: application/json' -d '{"mode":"error"}'
curl -X POST localhost:8090/admin/fault -H 'content-type: application/json' -d '{"mode":"none"}'

docker compose -f docker-compose.ft.yml down -v
```

## Stage E2: Alerting

### What was built, and why
Alerts turn "something is wrong" into a timestamped signal, so detection can be measured instead of guessed.

| Part | File, function | How it works |
|---|---|---|
| 7 alert rules | `prometheus/alerts.yml` | Loaded by both Prometheus configs (`rule_files`), evaluated every 5 s. Thresholds, reasons and responses: `docs/alerts.md` |
| Rule unit tests | `prometheus/alerts_test.yml` | promtool with SIMULATED series for AuthFailureSpike and RateLimitSpike (they need auth and rate limits from E3). Both fire above the threshold and stay silent below it |
| Alert recorder | `experiments/alert_watch.py`, `watch()` | Polls `/api/v1/alerts` every 1 s and writes each state change (pending, firing, resolved) with a timestamp to `results/endterm/alerts_<run>.csv`. Records the longest pause between two polls |
| Alert test | `experiments/alert_test.py`, `CASES`, `run_case()` | Per case: fresh stack, workload, fault at t=20 s. Each case lists the alerts that must fire and those that must not. Verdict PASS, FAIL or INVALID |
| Probe label | `obs.record_request()`, middleware in `common.create_app()`, `/ready` | `/ready` checks send `X-Probe: 1`. Those calls are counted with `source="probe"`, all other traffic with `source="user"`. nginx does not log them (`map $http_x_probe`). Rules and dashboard count only `source="user"` |
| Dashboard | `grafana/dashboards/university.json` | Error rate shows 0 instead of "No data". Default range is the last 5 minutes. Requests shows probes as a separate line. New "Alert history" panel shows which alert fired when |
| Finer latency buckets | `obs.BUCKETS` | Prometheus estimates a quantile inside a bucket, so the bucket edges must be close to real latencies (details below) |

Two different faults for the instance alerts (your decision, also in the report):
- `kill -9` of the process: the restart policy brings it back in 0.3 to 0.6 s. Used for availability numbers.
  InstanceDown (`up == 0` for 10 s) cannot fire on such a short outage. InstanceRestarted
  (`changes(process_start_time_seconds[1m]) > 0`) catches it.
- `docker stop` for 30 s: the instance is really gone. Used for the alert test and the demo. InstanceDown fires and resolves.

### Interrupted first series (not used)
The first full alert test series (started 2026-11-04, about 19:45) was ruined by the Mac sleeping:
`pmset -g log` shows "Entering Sleep state" many times from 19:41 on, and Docker Desktop was not running afterwards.
Signs: firing delays of 967 to 999 s in 150 s runs, resolution after 3066 s, two baseline cases only 19 s apart,
baseline alerts "did not fire" because the stack was down. The old script still wrote PASS for several of those rows,
because it did not check that a run was continuous.
- All files of that series were moved to `results/endterm/interrupted/` (README there explains why). None of it is used.
- Fix in `alert_test.py`: a run is INVALID if alert polling paused for more than 3 s, if it took more than 30 s longer
  than planned, or if fewer than 95% of the planned requests were sent.
- The series was rerun on 2026-11-06 with the laptop awake: every run had a poll gap of 1.0 s, ran 150 s
  (normal load 320 s) and sent all planned requests.

### Results (valid series, `results/endterm/alert_test.csv`, `alert_test_baseline.csv`)
Detection delay = fault injected -> alert `firing`. It is made of: up to 2 s until the next scrape, up to 5 s until the
next rule evaluation, plus the rule's `for` time (InstanceDown: 10 s).

| Case | Mode | Alert | Fired after | Resolved after | Verdict |
|---|---|---|---|---|---|
| `kill -9` payment-service-1 | FT | InstanceRestarted | 5.1 s | 60.4 s | PASS |
| | FT | InstanceDown | never (not even pending), correct: the outage was masked | | |
| `docker stop` 30 s | FT | InstanceDown | 17.2 s | 37.3 s | PASS |
| | FT | InstanceRestarted (process started again) | 37.3 s | 62.5 s | |
| bank `error` 30 s | FT | CircuitBreakerOpen | 4.1 s | 39.4 s | PASS |
| | FT | HighErrorRate | 9.2 s | 79.6 s | |
| | FT | HighLatencyP95 (not required, see findings) | 4.1 s | 34.3 s | |
| bank `delay 1 s` 45 s | FT | HighLatencyP95 | 6.1 s | 76.6 s | PASS |
| normal load 320 s, no fault | FT | none, not even pending (6400 requests) | | | PASS |
| `kill -9` payment-service | Baseline | InstanceDown | 12.2 s | never, correct: nothing restarts the process | PASS |
| | Baseline | InstanceRestarted | never, correct: no restart | | |
| bank `error` 30 s | Baseline | HighErrorRate | 7.1 s | 62.5 s | PASS |
| | Baseline | CircuitBreakerOpen | never, correct: the baseline has no breaker | | |

The first valid series gave 14.2 s and 3.1 s for the two baseline cases, but the script wrote FAIL for the crash:
it expected every alert to resolve, which is wrong for a system without a restart policy. Fixed (`STAYS_FIRING`)
and both baseline cases rerun. Old output: `results/endterm/alert_test_log.txt`.

FT vs baseline: detection is about as fast in both modes (differences of a few seconds come from the 2 s scrape and
5 s evaluation steps). What differs is what there is to detect: in FT the crash is masked (only InstanceRestarted, no
user errors), in the baseline the instance stays down and InstanceDown fires until someone acts. The breaker signal
exists only in FT.

Also: `promtool test rules` for AuthFailureSpike and RateLimitSpike (simulated series): SUCCESS. These two rules get
real tests in E5.

Probe label check (`results/endterm/e2_probe_check.txt`): over one 30 s workload, Prometheus `source="user"` counters
matched the workload CSV exactly for all 5 traffic types (timetable 246, registration 61, payment 104, transcript 52,
student-service calls 189). In the same 35 s, 143 calls came from `/ready` checks and were counted as probes.

### Grafana screenshots (FT, 2026-11-06, 20:48 to 21:08)
Faults were held until each screenshot was taken, so this session is an illustration, not a measurement (the measured
numbers are the table above). Alert states of the session: `results/endterm/alerts_screenshots_ft.csv`.
The session's request log was not saved: the workload writes its CSV only when it finishes, and it was stopped early.

| # | Moment | What it shows |
|---|---|---|
| 1 | Normal load | All 7 instances UP, error rate 0, breakers 0, no alerts (retaken after the 1 ms / 2.5 ms bucket fix and the Alert history fix) |
| 2 | `kill -9` payment-service-1 (20:53:45) | Alert history: InstanceRestarted only, no InstanceDown. Instances up: all UP; the crash is one `up=0` sample (20:53:47) |
| 3 | `docker stop` payment-service-1 (20:56:11) | payment-service-1 DOWN, InstanceDown firing (pending after 2.3 s, firing after 12.3 s), payments continue via payment-service-2 |
| 4 | Started again (20:58:13) | InstanceDown resolved after 4.2 s. InstanceRestarted did not fire: the stop lasted 2 min, longer than the rule's 1 min window |
| 5 | Bank `error` (21:00:43) | Error rate: bank 100%, payment-service 91%, others 0. Both breakers 2. Payments: failed up. HighErrorRate and CircuitBreakerOpen fired after 5.1 s |
| 6 | Bank `delay 1 s` (21:03:53) | HighLatencyP95 fired after 5.1 s, breakers stay 0. The latency panel on this screenshot overstates p95: about 2.4 s for a real 1.0 s, because of the old buckets (fixed after the session, screenshot not retaken). Report latencies come from the workload CSV |

### Findings (say these before you are asked)
- **Replays of failed payments count as server errors.** Idempotency returns the original result, which for a failed
  payment is 502. After a bank fault the workload keeps resending some failed keys, so payment-service keeps returning
  a few 502 with no new failure. Measured in the screenshot session, slow-bank phase: 0 new failed payments, 5.4
  replays answered 502 in 30 s (4.3% of payment requests), and HighErrorRate fired twice (15 s and 5 s). True by the
  rule, but not a current problem. Open decision, see the end of this section.
- **CircuitBreakerOpen flaps during a long outage.** The breaker passes through half_open every 10 s. When a scrape
  catches it, the alert resolves and fires again 5 s later. No `for` on this rule on purpose: it follows the breaker
  exactly.
- **HighLatencyP95 also fires during bank errors.** Before the breaker opens, payments wait for retries with backoff:
  payment p95 513 ms during the fault, 19.9 ms before. Correct by the rule, but right at the threshold.
- **InstanceRestarted only sees fast restarts** (old start time must be inside the last minute). Long outages are
  covered by InstanceDown.
- **Latency panels are estimates.** Prometheus interpolates inside a histogram bucket. With the default
  edges a 1 s bank delay was shown as p95 2.4 s (bucket 1 to 2.5 s). With the added edges (1.1, 1.25, 1.5, 2, 3 s)
  the same delay shows 1095 ms against 1024 ms measured by the client (`results/endterm/e2_bucket_check.txt`).
  Exact latencies for the report always come from the workload CSV.
- **The rules were measured with the default buckets** (before the 1 ms / 2.5 ms and 1.1 to 3 s edges were added).
  The 0.25, 0.5 and 0.75 s edges never changed, so HighLatencyP95 (500 ms) behaves the same with the new buckets.

### How to demo
```bash
docker compose -f docker-compose.ft.yml up -d --build
open http://localhost:3000                         # dashboard: Alert history at the bottom
open http://localhost:9090/alerts                  # all 7 rules and their state
docker compose -f docker-compose.ft.yml run --rm workload python /exp/workload.py --duration 600 &
python3 experiments/alert_watch.py demo --seconds 300 &      # writes results/endterm/alerts_demo.csv

python3 experiments/inject.py app_crash ft         # InstanceRestarted only
docker stop uni-ft-payment-service-1-1             # InstanceDown after about 17 s
docker start uni-ft-payment-service-1-1            # resolved after a few seconds
curl -X POST localhost:8090/admin/fault -H 'content-type: application/json' -d '{"mode":"error"}'   # breaker + errors
curl -X POST localhost:8090/admin/fault -H 'content-type: application/json' -d '{"mode":"none"}'

python3 experiments/alert_test.py --cases instance_crash --modes ft   # one measured case, about 3 min
docker run --rm --entrypoint promtool -v "$PWD/prometheus:/p:ro" -w /p prom/prometheus:v2.55.1 test rules alerts_test.yml
```

### Decision after E2: replays get their own label
Replays of failed payments returned 502 and counted as new server errors (see findings). Chosen fix: the
response stays the original result (still 502), but `payment.replay()` adds `X-Replayed: 1`, and the middleware in
`common.create_app()` counts such responses with `source="replay"`. HighErrorRate and the error rate count only
`source="user"`; replays are separate series on the "Requests" and "Error rate" panels, so nothing is hidden.
Check (`results/endterm/e2_replay_check.txt`): bank error for 30 s, then 100 s healthy. HighErrorRate fired during
the fault and resolved, with no new firing afterwards. In the last 60 s: 0 user 5xx, 9 replayed 502 out of 26 replays
(before the fix these would have been 3.7% of payment requests, close to the 5% threshold).

## Stage E3: Security

Active only in the endterm stack (`SECURITY_MODE=on` in `docker-compose.ft.yml`). The baseline keeps the
midterm behavior: no login, no rate limits, no audit, hard-coded DB password ("before" picture).

### What was built
| Part | File, function | How it works |
|---|---|---|
| Login | `security.login()`, route `POST /auth/login` in `student.py` | Looks the user up, checks the password with bcrypt (in a thread, so other requests are not blocked), returns a JWT (HS256, 15 min). Unknown user and wrong password get the same answer; unknown users are checked against a dummy hash so both take about as long |
| Lockout | `security.login()`, columns `users.failed_logins`, `locked_until` | 5 failures in a row lock the account for 60 s (one UPDATE, safe with two instances). While locked, even the right password gets 401 "too many failed logins" |
| Token check | `security.current_user()` (FastAPI dependency on every business endpoint) | `Authorization: Bearer <token>`; signature, `exp`, `sub`, `role` required; the algorithm is fixed to HS256, so `alg: none` is refused |
| Authorization | `security.check_student()`, `security.check_admin()` | Students read and write only their own data. Admin and services may read any student. Writes (pay, register) only for the student, not even the admin. Batch transcripts and reconciliation: admin only |
| Token passing | `transcript.build_transcript()`, `common.http_client(auth=...)` | transcript-service passes the user's token on to student-service. The batch job uses its own short service token (`security.service_auth()`, role `service`, 60 s). Tokens are never sent to the bank |
| Input validation | `StrictPaymentIn`, `StrictRegistration`, `LoginIn`; `Path(ge, le)`; header `KEY`; `security.install()` | Exact types, ids in the seeded range (students 1 to 50, courses 1 to 10), amount 1 to 1,000,000 with 2 decimals, Idempotency-Key at most 64 safe characters, unknown fields refused: 422. DB value errors map to 422, never 500. Gateway body limit 16 KB: 413 |
| Rate limits | `gateway/nginx.ft.conf` | `limit_req` per client IP: login 5/s (burst 10), payments 20/s (burst 40), `nodelay`, JSON 429 answer |
| 429 counter | `services/gateway_exporter.py`, `access_log ... if=$rate_limited` | nginx writes each 429 as a JSON line to a shared volume; the exporter follows the file and exposes `rate_limited_total{route}`; Prometheus job `gateway` |
| Secrets | `.env` (git-ignored), `.env.example`, `docker-compose.ft.yml` | JWT secret and all passwords come from `.env`. Replication, backup and Grafana passwords too (`db/init/00_replication.sh`, `infra/replica-entrypoint.sh`) |
| Audit log | table `audit_log` (`db/init/03_security.sh`), `security.audit()` | Row + JSON log line for login success and failure, lockout, every 401/403, payment create, admin actions (batch, reconcile). The JSON line is the record if the DB is down |
| Least privilege in the DB | role `uni_app` (`db/init/03_security.sh`) | Services connect as `uni_app`: only the SELECT/INSERT/UPDATE each table needs. Not the owner, so no DROP or ALTER. No TRUNCATE or DELETE anywhere. `audit_log`: INSERT only, so append-only and not even readable by the app |
| Idempotency, abuse | `payment.check_same_student()` | Midterm idempotency keys reused. New: a key that belongs to another student's payment gets 409 (replaying it would have shown that payment) |
| Header hygiene | `gateway/nginx.ft.conf` | Gateway removes `X-Probe` (a client cannot hide its traffic from the alerts) and sets `X-Real-IP` (audit source IP) |
| Threat model | `docs/threat_model.md` | STRIDE table: threat, asset, scenario, control, how it is tested; known limitations |
| Proof | `experiments/security_check.py` | 89 checks against the running stack, see below |

Clients that had to learn to log in:
- `experiments/workload.py`: logs in all 50 students before the timed load (about 11 s, paced under the login
  limit), sends each request with the token of its student, renews tokens after 14 min, resends a payment key
  only for the same student, and retries a login on 429, 5xx or no answer.
- `experiments/locustfile.py`: each simulated user logs in as one student and retries on 429.
- `experiments/auth.py` (reads `.env`), used by `run_all.py` (readiness, admin batch call), `consistency_check.py`
  (reconciliation needs an admin token), `trace_request.sh`, `demo.sh`, `security_check.py`, `attack.py`.

Two changes to earlier stages, caused by E3:
- transcript-service's `/ready` checks student-service through the gateway route `/health/student-service`
  instead of `/students/1`, which now needs a login. So the "student-service (probes)" line in the Requests
  panel is gone: the probe no longer calls a business endpoint.
- AuthFailureSpike counts only `source="user"`.

### Decisions to be able to defend
- **The 429 counter lives at the gateway** (proposal above): nginx sends the 429 itself, so the number has to come
  from nginx. Rejected requests never reach a service.
- **Login sits in student-service** (it owns the students), no extra service.
- **Writes are owner-only, even for the admin**: least privilege; the admin manages, it does not pay.
- **Lockout answers 401**, counted as `auth_failures_total{reason="locked"}`, so it feeds AuthFailureSpike.
- **Login fails closed when the primary DB is down** (503): the failed-login counter cannot be written on the
  read-only replica. Found by the regression demo (see below).

### Proof (real runs, 2026-11-06)
`results/endterm/e3_security_check.txt`: **89 of 89 checks passed**.
| Section | What was shown |
|---|---|
| A login | right password 200, wrong 401, unknown user gets exactly the same answer |
| B allowed | own record, own transcript (token passed on), own payment, admin reads, admin batch job done with 50 transcripts via the service token |
| C tokens | no token, other secret, forged admin, expired, `alg: none`, garbage: all 401 |
| D authorization | another student's record, timetable, transcript, payment, registration: 403; another student's payment key: 409; admin endpoints as a student: 403; admin paying for a student: 403; `/admin/*`, `/admin/fault`, `/metrics` through the gateway: 404; mock-bank port on 127.0.0.1 only |
| E validation | 20 bad inputs (amount 0, too big, negative, 3 decimals, text; id as text, out of range, 2^63; extra and missing fields; SQL-like key and username; 200-character key; 5000-character password; malformed JSON): all 422, none 5xx. 20 KB body: 413 |
| F integrity | md5 of all payments and grades identical before and after every refused attempt |
| G lockout | 5 wrong passwords, then the right one: 401 "too many failed logins"; after 61 s: 200 |
| H rate limits | 40 logins at once: 11 x 200, 29 x 429. 120 payment reads at once: 41 x 200, 79 x 429. `rate_limited_total` went up by exactly the 108 that the client saw |
| I audit log | rows for login success, bad password, unknown user, locked, lockout, missing/invalid/expired token, forbidden, payment create, batch start; rows carry request_id and source IP |
| J DB privileges | as `uni_app`: DROP, TRUNCATE, ALTER, DELETE (payments, audit_log), UPDATE audit_log and reading audit_log all refused; normal read works |
| K secrets | none of the 7 `.env` values and none of the issued tokens in 2177 log lines; `.env` ignored and not tracked by git; no `.env` value in the service image files or image settings |
| L metrics | `auth_failures_total` counted for all 7 reasons |

Two mistakes in my own check script, fixed before the final run: a tuple compared as "one of" instead of "equal",
and a 2-minute counter window that also caught the 429s of the previous run (213 instead of 107). The final
check compares the counter just before and just after the burst.

Other runs:
- Workload with login, 30 s (`results/endterm/raw/e3_smoke_ft.csv`): 600/600 OK, p95 18.6 ms (E1: 16.9 ms);
  the 13 resent keys all got 200. Logging in 50 students takes about 11 s before the timed load starts.
- `trace_request.sh`, `check_observability.py ft`, `consistency_check.py ft`: all pass with tokens.
- **Regression found a real problem.** The first `demo.sh ft all` stopped in db_failure: the demo injected the fault
  10 s after starting the workload process, while the workload was still logging in. A login writes the
  failed-login counter, the primary was down, so the login got 503 and the workload stopped. Fixes: `demo.sh`
  waits for "workload started"; the workload retries a login on 429, 5xx or no answer. Decision kept: login fails
  closed while the primary is down (see `docs/threat_model.md`).
- Rerun (`results/endterm/e3_regression_demo.txt`): FT app_crash 900/900, `restarts=1`, PASS; FT db_failure
  reads 200, writes 503, 80.1%, PASS; FT lost_transaction breaker open, 8 pending reconciled, PASS; baseline
  app_crash `restarts=0`, FAIL (as expected).
- Baseline unchanged, fresh stack: other students' data without a token 200, no login route (404), payment for
  student 9999 gives 500 (no validation, midterm behavior), workload 400/400, no security tables.

### Grafana screenshots (endterm, 2026-11-06, 22:31 to 22:43)
Attacks were held until each screenshot was taken (illustration; measured numbers are in the check above).
Alert states: `results/endterm/alerts_screenshots_e3_ft.csv`; attack counts in `results/endterm/raw/screenshots_e3_attack_*.txt`.

| # | Moment | What it shows |
|---|---|---|
| 7 | `attack.py unauthorized` from 22:32:54 (about 2/s: no token, student1 reading student 2) | 401 and 403 about 55 to 60 per minute each, auth failures by reason `missing_token` and `forbidden`, AuthFailureSpike firing (about 8 s after the start), error rate 0 (client errors are not server errors). Resolved 49 s after the attack stopped |
| 8 | `attack.py ratelimit` from about 22:37:32 (about 40 payment reads/s, limit 20/s) | 429 about 1100 per minute (6172 of 12422 requests refused), RateLimitSpike firing (about 6 s after the start), error rate 0, 401/403 back to 0. Resolved 50 s after the flood stopped (seen in Prometheus; the alert recorder had already been stopped) |

`rate_limited_total` was also defined in every service (always 0, because nginx answers the 429). Removed after
the session; only gateway-exporter exposes it now.

### How to demo
```bash
docker compose -f docker-compose.ft.yml up -d --build      # needs .env (cp .env.example .env, fill in values)
python3 experiments/security_check.py                      # 89 checks, about 2.5 min
curl -s localhost:8080/students/1                          # 401, no token
TOKEN=$(cd experiments && python3 -c "import auth; print(auth.student_token(1))")
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/students/1    # 200, own record
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/students/2    # 403, another student
python3 experiments/attack.py unauthorized --seconds 90    # AuthFailureSpike, 401/403 panel
python3 experiments/attack.py ratelimit --seconds 90       # RateLimitSpike, 429 line
docker compose -f docker-compose.ft.yml exec postgres-primary psql -U uni -d uni \
  -c "SELECT ts, user_id, action, result, source_ip FROM audit_log ORDER BY id DESC LIMIT 10;"
```

## Stage E4: Automated tests

### What was built
| Part | File | How it works |
|---|---|---|
| One command | `run_tests.sh` | Unit tests in a container, then a fresh endterm stack, then the integration tests on this machine. `--keep` uses the running stack instead |
| Unit tests (75) | `tests/unit/` | Run inside `tests/Dockerfile` (Python 3.11 + the service libraries + pytest). They import the service modules directly; no network, no database |
| Integration tests (18) | `tests/integration/test_endterm.py` | Run on this machine with pytest from `.venv-tests` (inside the project, git-ignored) against the running endterm stack. They reuse `experiments/auth.py`, `security_check.py` helpers, `inject.bank_fault` and `consistency_check.check` |
| Acceptance criteria | docstring of each test, `tests/conftest.py` | The docstring is stored in the JUnit file as property `criterion` |
| Reports | `tests/report.py` | Merges both JUnit files into `results/endterm/tests_junit.xml`, writes `results/endterm/tests_summary.txt` and regenerates `docs/test_plan.md` (test, criterion, result of the last run) |

Unit tests:
- `test_retry.py`: recovery after transient errors, 3 attempts maximum, no retry for permanent errors, backoff
  0.1-0.2 s then 0.2-0.4 s, retries counted for `X-Attempts`, which errors count as transient.
- `test_circuit_breaker.py`: opens after the threshold, rejects without calling, failures must be in a row,
  half_open success closes and failure reopens, only one trial at a time, `circuit_breaker_state` metric. A fake
  clock replaces `time.time`, so no test waits 10 s.
- `test_security.py`: token round trip and 15 min lifetime; missing, expired, other-secret, `alg: none` and
  role-less tokens refused; every refusal counted and audited; service token; the read/write rules for
  students, admin and services; admin-only endpoints.
- `test_validation.py`: the strict models are used in SECURITY_MODE=on; 15 payment, 5 registration and 8 login cases.
- `test_config.py`: parses `nginx.ft.conf`: zones 5 r/s and 20 r/s per IP, burst 10 and 40 with `nodelay`, status 429,
  429 log for the exporter, `X-Probe` removed, `X-Real-IP` set, 16 KB body limit, no route for `/admin` or `/metrics`,
  no `limit_req` in the baseline; exporter route names.

Integration tests: no token 401 (6 endpoints), other student's data 403, admin endpoints 403, 6 invalid
payments 422, idempotent payment (one DB row, one bank charge), audit rows for a refusal and for a payment,
`X-Real-IP` and `X-Probe` spoofing, bad `X-Request-ID` replaced, no secret or token in any log, consistency after
bank `crash_after_debit` (reconciled, nothing pending), burst of 120 requests gets 429 counted exactly by
`rate_limited_total`.

### What the tests found (real, not planned)
- **A client could hide its requests from the gateway log.** Since E2 the gateway skipped logging requests with
  `X-Probe: 1`, and it read that header from the client. Metrics were right (the header is removed before the
  services), but the gateway log missed those requests. `test_x_probe_cannot_hide_traffic` failed on its first run.
  Fix in `gateway/observability.conf`: the gateway decides by path (`/health/student-service`, its own
  health route), never by a client header. The test passes now.
- **A test depended on the one before it.** `test_burst_gets_429` created a payment right after the bank crash
  test had opened the payment breaker, so it got 503 in the full run but passed alone. It now reads an existing
  payment. Three full runs in a row passed after the fix.
- `gateway_exporter.py` started its endless loop on import; it now runs only as a program, so its route function
  can be tested.

### Result (2026-11-06)
`./run_tests.sh`: **93 of 93 passed** (unit 75, integration 18). Files: `results/endterm/tests_junit.xml`,
`results/endterm/tests_summary.txt`, `docs/test_plan.md`.

### Limits
- Not automated here: the 60 s lockout (in `security_check.py`, section G) and the full fault scenarios (E5).
- The integration tests run against one fresh stack; a test that changes shared state (the bank crash) can
  affect the next one, as seen above. Each test now prepares what it needs.

### How to demo
```bash
./run_tests.sh                     # about 1.5 min, prints the summary
cat results/endterm/tests_summary.txt
open docs/test_plan.md             # every test, its criterion and the last result
```

## Stage E5: Experiments

### How the series was run
- `experiments/e5.py` runs everything; `experiments/e5_report.py` builds `results/endterm/summary.csv` and
  `results/endterm/experiments.md` (hypothesis, configuration, steps, expected, observed, repetitions, limitations)
  from the data. Raw files: `results/endterm/raw/e5/` (request logs, monitor, alerts, attacks, Locust, docker stats).
- Every run: fresh stack (down -v, up, seed data), warm-up 15 s, alert recorder, experiment, integrity check, down.
  One row per run is appended to `results/endterm/experiments.csv` right away; a rerun resumes.
- Validity as in E2: INVALID if the alert recorder paused more than 3 s, the run took much longer than planned or
  the workload sent less than 95% of its requests. **Result: 30 of 30 runs valid.**
- Repetitions (agreed, option B): 2 per experiment and mode, `alert_test` 1 (E2 has a second, independent series).
  Shortened only what does not change the measurement: warm-up 15 s, 75 s runs (15 s warm-up, 30 s fault,
  30 s after), shorter waits. `alert_test` kept full fault lengths. Series: 2026-11-06 23:41 to 2026-11-07 00:55.
- A shortened smoke run of every experiment came first (`results/endterm/e5_smoke/`, code check only, not used).

### Results (means of the valid runs; full tables in `results/endterm/experiments.md`)
| Experiment | FT (endterm) | Baseline |
|---|---|---|
| instance_failure (kill -9) | 100% success, recovery 0 s, InstanceRestarted after 4.9 s, integrity PASS | 83.9%, not recovered, InstanceDown after 16 s, FAIL |
| dependency_timeout bank (5 s for 30 s) | 90.7%, p95 17 ms, breaker opens, detected 4.8 s, recovered 42 s, PASS | 93.6%, p95 5017 ms, monitor does not see it (alert does: 10 s), FAIL (money taken for failed payments) |
| dependency_timeout db (tables locked 30 s) | 81.6%, recovered 20 s, PASS | 79.8%, recovered 20.5 s, FAIL |
| load_spike 300 users | 84.5% of all, **100% of requests that passed the limit**, 1843 rejected by rate limit, 0 5xx, 252 ok req/s, CPU 82% | 100%, 299 ok req/s, CPU 76% |
| alert_test | 7 of 7 rules fired as expected (3.8 to 14.5 s), 0 alerts in 5 min normal load | 4 of 4 as expected, 0 in normal load |
| unauthorized_access | 80 of 80 refused, 80 audit rows, data unchanged, AuthFailureSpike | 80 of 80 succeeded, data changed |
| abusive_requests | 0 of 2500 answered 5xx, normal users 100% | 22 answered 5xx, 1109 succeeded, normal users 100% |
| recovery_consistency | 94.1%, recovered 25 s, PASS (no duplicate, lost or pending payment) | 85.9%, not recovered, FAIL |

### Findings to explain (real numbers)
- **Slow bank: FT has lower success than the baseline (90.7% vs 93.6%)**, as in the midterm: the breaker rejects
  payments at once, while the baseline waits up to 5 s. But the baseline took money for payments it marked failed
  (integrity FAIL in both runs); FT kept the money right. p95 17 ms vs 5017 ms.
- **Slow DB: FT is barely better (81.6% vs 79.8%).** Every failed timetable request in FT hit the client's 10 s
  timeout (10 004 ms). With the tables locked on primary and replica, FT tries 3 times x (primary 2 s + replica 2 s),
  about 12 s, longer than the client waits, so the cache answer comes too late. Lesson: the retry budget must be
  shorter than the caller's timeout (a total deadline per request). Not changed in this stage.
- **load_spike: all load comes from one IP**, so the per-IP limit (20 payment requests/s) applies to the whole test.
  The 429 are the protection working: every request that passed the limit succeeded (100%), with p95 8 ms.
- **Baseline cannot detect a slow bank with the monitor** (its `/ready` does not check dependencies), but the
  HighLatencyP95 alert, added in both modes, does (10.4 s). Observability helps the baseline too.
- **Two measurement fixes after the series (no rerun needed, raw data unchanged):** "detection" and "instances down"
  also counted the stack shutdown after each run (every instance "down" about 64 s after the fault). Both are now
  computed only inside the load window, from the raw monitor logs; 2 runs changed (baseline bank: now "not
  detected"). `e5.py` is fixed for future runs.

### Screenshot (Grafana)
`load_spike` at 300 users (2026-11-06 about 23:36, endterm, illustration run, not part of the series): payments capped
at about 20 req/s by the limit, about 2400 x 429 per minute, RateLimitSpike firing, 0 5xx, p95 about 10 ms.

### How to demo
```bash
python3 experiments/e5.py --plan                 # runs and estimated time
python3 experiments/e5.py --only unauthorized_access --reps 1     # one short experiment, both modes
python3 experiments/e5_report.py && open results/endterm/experiments.md
```
