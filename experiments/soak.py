"""Soak test: 20 min of load per mode, a random failure every ~2 min (90-150 s).

The same seed gives the same failure sequence in both modes, so they are comparable.
Writes results/soak_<mode>_requests.csv, results/soak_<mode>_events.csv,
results/soak_<mode>_monitor.csv and results/soak_summary.csv.

Usage: python3 experiments/soak.py [--modes baseline,ft] [--minutes 20]
"""
import argparse
import csv
import os
import random
import time

import consistency_check
import measure
from inject import ROOT, inject
from run_all import compose, sleep_until, start_stack, start_workload

FAULTS = ["app_crash", "db_failure", "network_timeout", "node_failure", "lost_transaction"]
SEED = 42


def soak(mode, minutes):
    print(f"\n=== soak {mode}", flush=True)
    results = os.path.join(ROOT, "results")
    events_csv = os.path.join(results, f"soak_{mode}_events.csv")
    start_stack(mode, f"soak_{mode}_monitor.csv")
    duration = minutes * 60
    p, t0 = start_workload(mode, duration, f"/results/soak_{mode}_requests.csv")

    rng = random.Random(SEED)
    t = t0 + rng.uniform(60, 120)
    events = []
    while t < t0 + duration - 60:  # last fault ends before the run ends
        sleep_until(t)
        scenario = rng.choice(FAULTS)
        ts = time.time()
        fault = inject(scenario, mode, f"soak_{mode}")
        events.append({"ts": round(ts, 3), "t_s": round(ts - t0, 1), "scenario": scenario})
        print(f"t={ts - t0:.0f}s inject {scenario}", flush=True)
        with open(events_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "t_s", "scenario"])
            w.writeheader()
            w.writerows(events)
        if fault:
            fault.join()
        t += rng.uniform(90, 150)

    print(p.communicate()[0].strip().splitlines()[-1], flush=True)
    time.sleep(5)
    passed, reason = consistency_check.check(mode)
    rows = measure.load_requests(os.path.join(results, f"soak_{mode}_requests.csv"))
    summary = {"mode": mode, "minutes": minutes, "failures": len(events), **measure.counts(rows),
               "consistency": "PASS" if passed else "FAIL", "consistency_reason": reason}
    print(summary, flush=True)
    compose(mode, "down", "-v")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="baseline,ft")
    ap.add_argument("--minutes", type=float, default=20)
    args = ap.parse_args()
    # Resume: modes already in soak_summary.csv are skipped.
    path = os.path.join(ROOT, "results", "soak_summary.csv")
    summaries = list(csv.DictReader(open(path))) if os.path.exists(path) else []
    for mode in args.modes.split(","):
        if any(s["mode"] == mode for s in summaries):
            print(f"skip soak {mode} (already in {path})")
            continue
        summaries.append(soak(mode, args.minutes))
        with open(path, "w", newline="") as f:  # save after every mode
            w = csv.DictWriter(f, fieldnames=list(summaries[0]))
            w.writeheader()
            w.writerows(summaries)


if __name__ == "__main__":
    main()
