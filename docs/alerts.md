# Alerts

Rules: `prometheus/alerts.yml`. Prometheus scrapes every 2 s and evaluates the rules every 5 s.
`experiments/alert_watch.py` polls `/api/v1/alerts` every 1 s and writes every state change
(pending, firing, resolved) to `results/endterm/alerts_<run>.csv`.

Request-based rules only count new user traffic, `source="user"`. Excluded:
- `source="probe"`: calls made by `/ready` checks (header `X-Probe: 1`).
- `source="replay"`: a duplicate payment (same Idempotency-Key) answered with its stored result. The response
  itself is unchanged (a replayed failure is still 502), only the metric label differs. Replays stay visible as
  separate series on the dashboard ("Requests" and "Error rate" panels), so nothing is hidden. Reason: after a
  bank fault the workload keeps resending some failed keys, and their 502 made HighErrorRate fire with no new
  failure (twice in the E2 screenshot session).
- `/health`, `/ready`, `/metrics`.

| Alert | What it detects | Rule and threshold | Why this threshold | Expected response | How it is tested |
|---|---|---|---|---|---|
| InstanceDown | A service instance cannot be scraped | `up == 0` for 10 s | Ignores a single failed scrape (2 s interval). A crash healed by the restart policy in under 1 s does not count | Restart policy should bring it back. If not, check the container (`docker compose ps`, logs) | `docker stop` of payment-service-1 for 30 s |
| InstanceRestarted | The process started again, e.g. after a crash | `changes(process_start_time_seconds[1m]) > 0`, fires at once | Catches the crashes that InstanceDown misses because the restart takes 0.3 to 0.6 s | A crash was masked by the restart. Look for the cause in the logs before it happens again | `kill -9` of the payment-service-1 process |
| HighErrorRate | Many requests fail on the server side | 5xx / all requests > 5% over 30 s, per service | 5% is far above normal (0 in our runs) but low enough to catch a broken dependency | Check the failing service and its breaker state | mock-bank in `error` mode for 30 s |
| HighLatencyP95 | Requests are slow | p95 > 500 ms over 30 s, per service | Normal p95 is about 17 ms. 500 ms is noticeable for a user | Check the load and any slow dependency (bank, DB) | mock-bank `delay 1 s` for 45 s (below the 2 s timeout, so calls are slow but do not fail) |
| CircuitBreakerOpen | A breaker stopped calls to a dependency | `circuit_breaker_state == 2`, fires at once | Open means calls are already being rejected | Check the dependency (bank or student-service) | Same bank `error` fault |
| AuthFailureSpike | Many failed logins or forbidden requests | 401 + 403 > 20 per minute | Normal users rarely fail. 20 per minute suggests guessing | Possible brute force. Check the audit log, consider blocking the source | Needs auth from stage E3. Until then: promtool unit test with simulated series |
| RateLimitSpike | Many requests rejected with 429 | 429 > 50 per minute | The limits are high enough that normal use stays well below this | Possible abuse. Block the source | Needs rate limiting from stage E3. Until then: promtool unit test with simulated series |

## Measured (results/endterm/alert_test.csv, valid series of 2026-11-06)
Detection delay = injection to `firing` in the Prometheus alerts API (polled every 1 s).
It is made of: up to 2 s until the next scrape, up to 5 s until the next rule evaluation, plus
the rule's `for` time. Every run was checked for continuity (alert poll gap 1.0 s, run 150 s,
all planned requests sent).

| Case (fault at t=20 s) | Expected | FT: fired after | FT: resolved after | Verdict |
|---|---|---|---|---|
| instance_crash: `kill -9` payment-service-1 | InstanceRestarted, not InstanceDown | InstanceRestarted 5.1 s, InstanceDown never (not even pending) | 60.4 s | PASS |
| instance_stop: `docker stop` 30 s | InstanceDown | InstanceDown 17.2 s, then InstanceRestarted 37.3 s (the process started again) | 37.3 s / 62.5 s | PASS |
| bank_error: bank `error` 30 s | HighErrorRate, CircuitBreakerOpen | CircuitBreakerOpen 4.1 s, HighErrorRate 9.2 s, also HighLatencyP95 4.1 s (see below) | 39.4 s / 79.6 s / 34.3 s | PASS |
| bank_slow: bank `delay 1 s` 45 s | HighLatencyP95 | HighLatencyP95 6.1 s | 76.6 s | PASS |
| normal_load: 320 s, 6400 requests, no fault | nothing | no alert at all, not even pending | | PASS |

| Case (fault at t=20 s) | Expected | Baseline: fired after | Baseline: resolved after | Verdict |
|---|---|---|---|---|
| instance_crash: `kill -9` payment-service | InstanceDown, stays firing (no restart policy), not InstanceRestarted | InstanceDown 12.2 s | never, correct: the process stays dead | PASS |
| bank_error: bank `error` 30 s | HighErrorRate, not CircuitBreakerOpen (no breaker in baseline) | HighErrorRate 7.1 s | 62.5 s | PASS |

The first valid series gave 14.2 s (InstanceDown) and 3.1 s (HighErrorRate) for the same baseline cases. The script
then wrote FAIL for the crash, because it expected every alert to resolve, which is wrong for a system without a
restart policy. Fixed (`STAYS_FIRING` in `alert_test.py`) and both baseline cases rerun; the old output is kept in
`results/endterm/alert_test_log.txt`. Differences of a few seconds come from the 2 s scrape and 5 s evaluation steps.

## Two different faults for "instance" alerts
- `kill -9` of the app process (`instance_crash`): Docker restarts it in 0.3 to 0.6 s. This is the
  fault used for availability numbers (midterm and E5). InstanceDown is not expected to fire, because
  the outage is shorter than one scrape plus 10 s. InstanceRestarted fires.
- `docker stop` for 30 s (`instance_stop`): the instance is really gone. InstanceDown fires and
  resolves when the instance is started again. This fault is used for the alert test and the demo.

## Unit tests with simulated data
`prometheus/alerts_test.yml` (promtool) checks AuthFailureSpike and RateLimitSpike with SIMULATED
series: once above the threshold (must fire) and once below (must not fire).
```bash
docker run --rm --entrypoint promtool -v "$PWD/prometheus:/p:ro" -w /p prom/prometheus:v2.55.1 test rules alerts_test.yml
```
These two rules are tested on real traffic in stage E5 (unauthorized_access, abusive_requests).

## Findings (say these before you are asked)
- **InstanceRestarted only sees fast restarts.** `changes(...[1m])` needs the old start time inside the last
  minute. After a 30 s stop it fired; after a 2 min stop (screenshot session) it did not. That is fine:
  long outages are what InstanceDown is for, and it fired.
- **One failed scrape is not an outage.** The `kill -9` crash shows as exactly one `up=0` sample
  (2 s resolution) on the dashboard. InstanceDown needs 10 s, so it correctly stays silent.
- **CircuitBreakerOpen flaps during a long dependency outage.** The breaker goes open -> half_open -> open
  every 10 s; when a scrape catches half_open (value 1) the alert resolves and fires again 5 s later.
  It has no `for`, on purpose, so the alert follows the breaker exactly. A `for: 15s` would remove the
  flapping but delay detection.
- **HighLatencyP95 also fires during bank errors.** Before the breaker opens, payments wait for retries
  with backoff: payment p95 was 513 ms during the fault (19.9 ms before), just above the 500 ms threshold.
  A true alert by the rule, but borderline: in the screenshot session it fired only once, for 10 s.
- **Replays of failed payments count as server errors.** Idempotency returns the original result, and for
  a failed payment that is 502. After a bank fault the workload keeps resending some failed keys
  (10% of payments are resends), so payment-service keeps returning a few 502 with no new failure.
  Measured during the slow-bank phase of the screenshot session: 0 new failed payments, 5.4 replays
  answered 502 in 30 s (4.3% of payment requests). HighErrorRate fired twice for 15 s and 5 s. True by
  the rule, but not a current problem: noise. Fix to decide (see DEFENSE_NOTES, stage E2).
- **Latency panels are estimates.** Prometheus interpolates inside a histogram bucket. With the old
  buckets a 1 s bank delay showed as p95 2.4 s (bucket 1 to 2.5 s). Buckets now include 1.1, 1.25,
  1.5, 2 and 3 s: the same delay shows 1095 ms against 1024 ms measured by the client
  (`results/endterm/e2_bucket_check.txt`). Exact latencies for the report come from the workload CSV.
  The 0.25, 0.5 and 0.75 s edges never changed, so HighLatencyP95 (500 ms) behaves exactly as measured.
