"""Polls the Prometheus alerts API every 1 s and writes state changes to CSV.

Usage: python3 experiments/alert_watch.py <run> [--seconds N]   writes results/endterm/alerts_<run>.csv
States: pending, firing, resolved (the alert left the API).
"""
import argparse
import csv
import json
import os
import threading
import time
import urllib.request

from inject import ROOT

API = "http://localhost:9090/api/v1/alerts"
FIELDS = ["ts", "alertname", "instance", "service", "target", "state"]


def active_alerts():
    data = json.load(urllib.request.urlopen(API, timeout=2))["data"]["alerts"]
    return {tuple(sorted(a["labels"].items())): a["state"] for a in data}


def watch(run, stop, stats=None, folder=None):
    """Runs until the threading.Event `stop` is set. Returns the rows it wrote.
    stats["max_gap_s"]: longest time between two successful polls. A large gap means the
    laptop slept or Prometheus did not answer, so the run cannot be trusted."""
    folder = folder or os.path.join(ROOT, "results", "endterm")
    os.makedirs(folder, exist_ok=True)
    rows, last = [], {}
    stats = {} if stats is None else stats
    stats["max_gap_s"], last_ok = 0.0, None
    with open(os.path.join(folder, f"alerts_{run}.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()

        def change(key, state):
            labels = dict(key)
            row = {"ts": round(time.time(), 3), "alertname": labels.get("alertname"), "instance": labels.get("instance", ""),
                   "service": labels.get("service", ""), "target": labels.get("target", ""), "state": state}
            w.writerow(row)
            f.flush()
            rows.append(row)

        while not stop.is_set():
            try:
                now = active_alerts()
            except OSError:  # Prometheus not reachable right now: keep the last known states
                stop.wait(1)
                continue
            t = time.time()
            if last_ok is not None:
                stats["max_gap_s"] = max(stats["max_gap_s"], t - last_ok)
            last_ok = t
            for key, state in now.items():
                if last.get(key) != state:
                    change(key, state)
            for key in last:
                if key not in now:
                    change(key, "resolved")
            last = now
            stop.wait(1)
    return rows


def watch_in_background(run, folder=None):
    """Returns (stop event, thread, result list, stats dict)."""
    stop, result, stats = threading.Event(), [], {}
    t = threading.Thread(target=lambda: result.extend(watch(run, stop, stats, folder)))
    t.start()
    return stop, t, result, stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--seconds", type=float)
    args = ap.parse_args()
    stop = threading.Event()
    if args.seconds:
        threading.Timer(args.seconds, stop.set).start()
    try:
        watch(args.run, stop)
    except KeyboardInterrupt:
        stop.set()
