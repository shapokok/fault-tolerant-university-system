# Endterm: Resilient University Information System

## Goal
Extend the midterm project (same repo, same services, same workflow). Do not start a new system.
Add: observability, alerting, security controls, automated tests, and at least 7 measured
experiments, all compared with the midterm baseline. Every claim must be backed by a real run
(CSV, logs, metrics export). Never fabricate or estimate a number. Label anything simulated.

## Modes (keep the midterm comparison fair)
- baseline: docker-compose.baseline.yml, FT_MODE=off, SECURITY_MODE=off (midterm baseline, unchanged
  except metrics/logs, which are ON in both modes so both can be measured the same way).
- endterm: docker-compose.ft.yml, FT_MODE=on, SECURITY_MODE=on.
Observability is a measurement tool, not a resilience mechanism, so it runs in both modes.

## Stage E1: Observability
1. Metrics (prometheus_client, GET /metrics on every service instance):
   - http_requests_total{service, instance, endpoint, method, status}
   - http_request_duration_seconds histogram{service, endpoint}
   - circuit_breaker_state{service, target} (0 closed, 1 half_open, 2 open)
   - payments_total{status} (completed, pending, failed, replayed)
   - auth_failures_total{reason}, rate_limited_total
2. Structured JSON logs to stdout: ts, level, service, instance, request_id, method, path,
   status, latency_ms, user_id (if known), event, message.
3. Correlation ID: nginx sets X-Request-ID ($request_id) if missing; every service reads it,
   puts it in logs and response headers, and forwards it on every outgoing httpx call.
   Add experiments/trace_request.sh: send one transcript request and grep its request_id
   in the logs of gateway, transcript-service and student-service.
4. Prometheus (scrape every 2 s, evaluation every 5 s) and Grafana in both compose files.
   Grafana provisioned automatically (datasource + dashboard JSON in grafana/), anonymous viewer on.
   Dashboard panels: req/s per service, error rate %, p50/p95 latency, instances up,
   breaker state, payments by status, 401/403/429 counts, firing alerts.

## Stage E2: Alerting
prometheus/alerts.yml with at least these rules (keep thresholds simple, document each):
- InstanceDown: up == 0 for 10s. Response: restart policy / check container.
- HighErrorRate: 5xx ratio > 5% over 30s. Response: check failing service, breaker state.
- HighLatencyP95: p95 > 500 ms over 30s. Response: check load and slow dependency.
- CircuitBreakerOpen: circuit_breaker_state == 2. Response: check the dependency (bank, student).
- AuthFailureSpike: 401+403 > 20 per minute. Response: possible brute force, check audit log.
- RateLimitSpike: 429 > 50 per minute. Response: possible abuse, block source.
experiments/alert_watch.py polls the Prometheus /api/v1/alerts API every 1 s and writes
state changes (pending, firing, resolved) with timestamps to results/endterm/alerts_<run>.csv.
docs/alerts.md: table with alert, what it detects, threshold, expected response.

## Stage E3: Security (active only when SECURITY_MODE=on)
1. Authentication: POST /auth/login (synthetic users seeded: 50 students, 1 admin, bcrypt hashes)
   returns a JWT (HS256, 15 min). Gateway passes Authorization to services, services verify.
2. Authorization (least privilege): a student can read and pay only for their own student_id;
   admin endpoints (/admin/*, batch transcripts, reconcile) need role admin. mock-bank /admin/fault
   is not routed through the gateway (internal network only).
3. Input validation: Pydantic models with strict types and limits (amount 1..1,000,000,
   ids in seeded range, string lengths), extra fields forbidden. Invalid input = 422, never 500.
4. Rate limiting: nginx limit_req per client IP on /auth/login (5 r/s, burst 10) and /payments
   (20 r/s, burst 40), returns 429. Login lockout: 5 failed logins for one user = 60 s lock.
5. Secrets: JWT secret, DB passwords from .env (not committed), commit .env.example only.
   No secrets in code, logs or images. A test checks that no secret appears in logs.
6. Audit log: table audit_log (ts, request_id, user_id, action, resource, result, source_ip)
   plus JSON log line for: login success/failure, lockout, payment create, 401/403,
   admin actions. Append-only for the app DB user.
7. Least privilege in DB: app role without DROP/TRUNCATE/ALTER, no DELETE on audit_log.
8. Duplicate/abuse protection: reuse midterm idempotency keys.
docs/threat_model.md: STRIDE table (threat, asset, scenario, control, how it is tested).
The workload generator must log in and use tokens when SECURITY_MODE=on.

## Stage E4: Automated tests
tests/unit: retry, CircuitBreaker, JWT create/verify, validation models, rate-limit config parse.
tests/integration (run against the running endterm stack): idempotent payment, no token = 401,
other student's data = 403, invalid payload = 422, burst = 429, audit row written, consistency
check passes after a fault. One command: ./run_tests.sh, writes results/endterm/tests_junit.xml
and a short summary results/endterm/tests_summary.txt. Each test has an acceptance criterion
in docs/test_plan.md (test, criterion, pass/fail from the last run).

## Stage E5: Experiments (both modes where it makes sense, 3 repetitions each)
Restore a known state before every run (down -v, up, seed, warm-up 15 s).
Workload 20 req/s unless stated. Record per run: total, success, failed, error rate, avg and p95
latency, detection time, recovery time, alerts fired (and whether correct), integrity result.
1. instance_failure: kill -9 payment-service-1 (as midterm).
2. dependency_timeout: mock-bank delay 5 s for 30 s, and DB query delay (pg_sleep or tc) for 30 s.
3. load_spike: Locust stepped 50 -> 150 -> 300 users, 60 s each; throughput, p50/p95, error rate,
   CPU/memory from docker stats.
4. alert_test: trigger each alert rule once on purpose; measure detection delay from
   injection to firing; also run 5 min normal load and confirm 0 false alerts.
5. unauthorized_access: no token, forged token, expired token, student A reading/paying for
   student B, student calling admin endpoint. Expect 401/403, audit rows, and DB checksum of
   payments/grades unchanged (prove no data exposed or changed).
6. abusive_requests: 500 malformed payloads (fuzz types, sizes, missing fields, SQL-like strings)
   and a 1,000-request burst on /auth/login and /payments. Expect 422/429, no 5xx, service up,
   integrity check passes.
7. recovery_consistency: crash_after_debit during payments and kill payment-service mid-request;
   expect no duplicate or lost payments after reconciliation (midterm consistency_check.py).
Outputs: results/endterm/experiments.csv (one row per experiment x mode x repetition),
results/endterm/summary.csv (mean and std per experiment x mode), results/endterm/raw/,
results/endterm/experiments.md generated from the data with, for each experiment:
hypothesis, configuration, steps, expected, observed, repetitions, limitations.

## Stage E6: Analysis and comparison
analysis/endterm.py: baseline vs endterm table for every experiment; comparison with midterm
results/experiments.csv where scenarios match; plots (success rate over time with injection
marked, latency p95, alert timelines) to results/endterm/plots/; Prometheus query_range export
to CSV for the dashboard panels used in the report. Updated architecture diagram
(results/endterm/plots/architecture_endterm.png) showing Prometheus, Grafana, auth, rate limit,
audit log. Tell me which Grafana screenshots to take manually and when.

## Stage E7: Demo and README
demo_endterm.sh with 3 scenarios: instance_failure (alert fires and resolves), unauthorized_access
(403 + audit row), abusive_requests (429 + service stays up + integrity PASS). Update README with
run instructions for both modes, tests, experiments and dashboard URL.

## Rules
- Work stage by stage. After each stage: run it, show proof, stop and wait for my OK.
- Keep code simple; I must explain it in the oral defense.
- After each stage append to docs/DEFENSE_NOTES.md: what was built, why, which file/function,
  how to demo it. Plain language.
- Never use em dashes in any text, code comments or docs.
- Do not fake results. If something does not work or a number looks wrong, say so.
- Synthetic data only. Do not touch anything outside this project.