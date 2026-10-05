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
