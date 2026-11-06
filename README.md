# Resilient University Information System

A small distributed university platform, built in two versions and compared with real measurements:

- **baseline** (`docker-compose.baseline.yml`): one instance per service, no fault tolerance, no security
  (the midterm baseline). Metrics, logs and alerts are on, so it can be measured the same way.
- **endterm** (`docker-compose.ft.yml`): two instances per service, database replica, self-healing,
  software fault tolerance (midterm), plus security: login, authorization, input validation, rate limits,
  audit log, secrets in `.env`, least-privilege database role.

All numbers in the reports come from real runs (`results/`). The midterm is on branch `main`; this branch
(`endterm`) extends it.

## Stack
Python 3.11, FastAPI, httpx, asyncpg, PostgreSQL 16 (primary + streaming replica), nginx, Prometheus, Grafana,
Docker Compose, Locust, pytest, pandas + matplotlib. One laptop, no Kubernetes.

## Services
| Service | Role |
|---|---|
| gateway (nginx, `localhost:8080`) | single entry point, load balancer; endterm: rate limits, header rules |
| student-service | students, registrations, timetable (cache), login (`POST /auth/login`) |
| transcript-service | transcripts (calls student-service), batch generation with checkpoints |
| payment-service | payments (calls mock-bank), idempotency, reconciliation |
| mock-bank (`localhost:8090`) | fake bank with `POST /admin/fault {mode: none\|delay\|error\|crash_after_debit}` (endterm: 127.0.0.1 only) |
| postgres-primary / postgres-replica | database, streaming replication |
| monitor | polls `/health` and `/ready` of every instance every 0.5 s |
| prometheus (`localhost:9090`), grafana (`localhost:3000`) | metrics, 7 alert rules, dashboard (both modes) |
| gateway-exporter (endterm) | counts the 429 answers nginx sends (`rate_limited_total`) |
| backup (endterm) | `pg_dump` every 60 s into `backups/` |

## Requirements
- Docker Desktop (Compose v2).
- `python3` on the machine for the scripts (standard library), `pandas` and `matplotlib` for the analysis.
  `./run_tests.sh` creates its own `.venv-tests` with pytest.
- For the endterm stack: a `.env` file with the secrets (never committed):
  ```bash
  cp .env.example .env
  # put a new random value in every line, for example:
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

## Run
```bash
# endterm (fault tolerance + security)
docker compose -f docker-compose.ft.yml up -d --build
TOKEN=$(cd experiments && python3 -c "import auth; print(auth.student_token(1))")   # login as student1
curl -H "Authorization: Bearer $TOKEN" localhost:8080/timetable/1                 # 200
curl localhost:8080/timetable/1                                                    # 401, no token
curl -H "Authorization: Bearer $TOKEN" localhost:8080/students/2                  # 403, another student
docker compose -f docker-compose.ft.yml down -v

# baseline (midterm behavior, no login)
docker compose -f docker-compose.baseline.yml up -d --build
curl localhost:8080/timetable/1
docker compose -f docker-compose.baseline.yml down -v
```
Only one stack can run at a time (both use ports 8080, 8090, 9090 and 3000).

**Dashboard:** http://localhost:3000 (Grafana, no login; dashboard "University platform").
**Alerts:** http://localhost:9090/alerts (Prometheus). Rules and responses: `docs/alerts.md`.

## Demos
```bash
./demo_endterm.sh             # endterm: unauthorized access, abusive requests, instance failure (alert fires and resolves)
./demo_endterm.sh abusive     # one scenario: unauthorized | abusive | instance
./demo.sh ft all              # midterm demo: app_crash, db_failure, lost_transaction (works on the endterm stack)
./demo.sh baseline app_crash  # the same fault on the baseline
DEMO_FAST=1 ./demo_endterm.sh # no pauses
```
`demo_endterm.sh` takes about 4 min: one fresh endterm stack, then the three scenarios with a pause before each.

## Tests
```bash
./run_tests.sh          # 75 unit tests (container) + 18 integration tests (fresh endterm stack), about 1.5 min
./run_tests.sh --keep   # integration tests against the endterm stack that is already running
```
Results: `results/endterm/tests_junit.xml`, `results/endterm/tests_summary.txt`; every test with its acceptance
criterion and last result: `docs/test_plan.md`. Security checks with more detail: `python3 experiments/security_check.py`.

## Experiments and analysis
```bash
python3 experiments/e5.py --plan     # the endterm series: runs and estimated time
python3 experiments/e5.py            # 7 experiments x 2 modes (about 1.5 h, resumes if stopped)
python3 experiments/e5_report.py     # results/endterm/summary.csv and experiments.md
python3 analysis/prometheus_export.py   # one endterm session, dashboard panels as CSV
python3 analysis/endterm.py          # comparison tables, analysis.md, plots (black and white friendly)
python3 experiments/alert_test.py    # alert rules, one fault per rule (stage E2)
python3 experiments/run_all.py       # midterm experiments; analysis/metrics.py for the midterm analysis
```

## Results (endterm, mean of the valid E5 runs)
| Experiment | Baseline | Endterm |
|---|---|---|
| Instance crash (kill -9) | 83.9% success, not recovered | 100.0%, recovered at once |
| Slow bank (5 s for 30 s) | 93.6%, money taken for failed payments | 90.7%, money consistent |
| Slow DB (tables locked 30 s) | 79.8% | 81.6% (retry budget too long, see analysis) |
| Load spike, 300 users from one IP | 100% | 84.5% of all, 100% of requests that passed the rate limit |
| Unauthorized access (80 attempts) | 80 succeeded | 80 refused, 80 audit rows, data unchanged |
| Abusive requests (2500) | 22 answered 5xx | 0 answered 5xx, normal users 100% |
| Alert test | 4 of 4 rules as expected | 7 of 7 rules as expected, 0 alerts in 5 min of normal load |

Full tables, discussion and lessons learned: `results/endterm/analysis.md`, per experiment:
`results/endterm/experiments.md`, plots: `results/endterm/plots/`.

## Where things are
| Path | Content |
|---|---|
| `services/` | service code; `obs.py` metrics, logs, request IDs; `security.py` login, tokens, authorization, audit; `ft.py` retry, circuit breaker |
| `gateway/` | nginx configs (baseline, endterm), JSON access log |
| `prometheus/`, `grafana/` | scrape configs, alert rules and their tests, provisioned dashboard |
| `db/init/` | schema, seed data, replication role, endterm security objects (`03_security.sh`) |
| `infra/` | failover, backup, restore, replica entrypoint |
| `experiments/` | workload, fault injection, alert watcher and tests, security check, E5 runner, demos |
| `tests/` | unit and integration tests, report generator |
| `analysis/` | midterm and endterm analysis, Prometheus export |
| `results/` | midterm results; `results/endterm/` for the endterm |
| `docs/` | `DEFENSE_NOTES.md` (every stage), `alerts.md`, `threat_model.md`, `test_plan.md`, `hardware_ft.md` |
