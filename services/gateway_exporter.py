"""Counts the requests the nginx rate limiter rejected with 429 (stage E3).

nginx answers those requests itself, so no service ever sees them. nginx writes one JSON line
per 429 to a file on a shared volume; this script follows the file and exposes
rate_limited_total{route} on port 8000 (/metrics) for Prometheus.
"""
import json
import os
import time

from prometheus_client import Counter, disable_created_metrics, start_http_server

disable_created_metrics()
LOG = "/logs/rate_limited.json"
RATE_LIMITED = Counter("rate_limited_total", "Requests rejected by the nginx rate limiter (429)", ["route"])
ROUTES = ("/auth/login", "/payments")


def route(path):
    return next((r for r in ROUTES if path.startswith(r)), "other")


def main():
    for r in ROUTES:
        RATE_LIMITED.labels(route=r)  # start at 0, so the first 429s are counted by increase()
    start_http_server(8000)
    while not os.path.exists(LOG):
        time.sleep(0.5)
    with open(LOG) as f:
        f.seek(0, os.SEEK_END)  # after a restart, do not count old lines a second time
        line = ""
        while True:
            line += f.readline()
            if not line.endswith("\n"):  # nothing new yet, or nginx is still writing the line
                time.sleep(0.2)
                continue
            try:
                RATE_LIMITED.labels(route=route(json.loads(line)["path"])).inc()
            except (ValueError, KeyError):
                pass  # not a log line we understand
            line = ""


main()
