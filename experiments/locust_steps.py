"""Stepped load for E5 load_spike: 50 -> 150 -> 300 users, 60 s each (same users as locustfile.py).

Every request is written to LOCUST_LOG (start time, endpoint, status, latency), so 429 can be
counted on its own and every step can be measured exactly. LOCUST_STEP_S changes the step length.
LOCUST_HOLD=1 runs 50 and 150 as usual, then stays at 300 users (screenshot session) until stopped.
"""
import csv
import os
import time

from locust import LoadTestShape, events
from locustfile import Student  # noqa: F401  (the simulated user)

STEPS = (50, 150, 300)
STEP_S = float(os.getenv("LOCUST_STEP_S", "60"))
SPAWN_RATE = 50
LOG = []


class Steps(LoadTestShape):
    def tick(self):
        t = self.get_run_time()
        step = int(t // STEP_S)
        if os.getenv("LOCUST_HOLD") == "1" and t < 3600:
            step = min(step, len(STEPS) - 1)
        return (STEPS[step], SPAWN_RATE) if step < len(STEPS) else None


@events.request.add_listener
def on_request(name, response_time, response=None, exception=None, **kwargs):
    status = getattr(response, "status_code", 0) or 0
    LOG.append((round(time.time() - response_time / 1000, 3), name, status, round(response_time, 1)))


@events.test_stop.add_listener
def write_log(**kwargs):
    with open(os.environ["LOCUST_LOG"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "endpoint", "status", "latency_ms"])
        w.writerows(LOG)
