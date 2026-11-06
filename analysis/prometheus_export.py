"""Exports every dashboard panel as CSV (Prometheus query_range) for one representative endterm session.

Session (about 5.5 min, endterm stack): normal load; bank error 30 s at +60 s; 401/403 flood 30 s at +150 s;
rate limit flood 30 s at +240 s. Writes results/endterm/prometheus/<panel>.csv (time, series, value)
and session_events.csv. Run from the project root: python3 analysis/prometheus_export.py
"""
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "experiments"))
import auth  # noqa: E402
from e5 import bank_for, in_background, rate_flood, unauthorized_flood  # noqa: E402
from run_all import compose, sleep_until, start_stack, start_workload  # noqa: E402
from security_check import call  # noqa: E402

OUT = os.path.join(ROOT, "results", "endterm", "prometheus")
STEP_S = 2


def query_range(expr, start, end):
    url = "http://localhost:9090/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": expr, "start": start, "end": end, "step": STEP_S})
    return json.load(urllib.request.urlopen(url, timeout=30))["data"]["result"]


def legend(fmt, labels):
    return re.sub(r"\{\{(\w+)\}\}", lambda m: labels.get(m.group(1), ""), fmt) or json.dumps(labels)


def main():
    os.makedirs(OUT, exist_ok=True)
    start_stack("ft", "endterm/prometheus/session_monitor.csv")
    token = auth.student_token(1)
    pid = json.loads(call("POST", "/payments", {"student_id": 1, "amount": 5}, token, {"Idempotency-Key": uuid.uuid4().hex})[1])["payment_id"]
    p, t0 = start_workload("ft", 330, "/results/endterm/prometheus/session_workload.csv")
    events = []
    for at, name, fault in [(60, "bank error 30 s", lambda: bank_for("error", 30)),
                            (150, "401/403 flood 30 s", lambda: unauthorized_flood(token, 30)),
                            (240, "rate limit flood 30 s", lambda: rate_flood(token, pid, 30))]:
        sleep_until(t0 + at)
        events.append({"ts": round(time.time(), 3), "t_s": at, "event": name})
        in_background(fault).join()
    p.communicate()
    time.sleep(5)
    end = time.time()
    with open(os.path.join(OUT, "session_events.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "t_s", "event"])
        w.writeheader()
        w.writerows([{"ts": round(t0, 3), "t_s": 0, "event": "workload start"}] + events + [{"ts": round(end, 3), "t_s": round(end - t0), "event": "end"}])
    dash = json.load(open(os.path.join(ROOT, "grafana", "dashboards", "university.json")))
    for panel in dash["panels"]:
        name = re.sub(r"[^a-z0-9]+", "_", panel["title"].lower()).strip("_")[:50]
        with open(os.path.join(OUT, f"{name}.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "t_s", "series", "value", "query"])
            for target in panel["targets"]:
                for series in query_range(target["expr"], t0, end):
                    label = legend(target.get("legendFormat", ""), series["metric"])
                    for ts, value in series["values"]:
                        w.writerow([ts, round(ts - t0, 1), label, value, target["expr"]])
        print("exported", name, flush=True)
    compose("ft", "down", "-v")


if __name__ == "__main__":
    main()
