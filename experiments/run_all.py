"""Runs every scenario in both modes and writes results/experiments.csv.

Per run: fresh stack -> workload 90 s -> transcript batch at t=15 s -> inject at t=20 s
-> wait for the end -> consistency check -> metrics.

Usage: python3 experiments/run_all.py [--scenarios a,b] [--modes baseline,ft] [--fresh]
By default runs already in results/experiments.csv are skipped (resume).
"""
import argparse
import csv
import os
import subprocess
import time
import urllib.request

import consistency_check
import measure
from inject import ROOT, SCENARIOS, inject

RUN_S, BATCH_AT, INJECT_AT = 90, 15, 20
FIELDS = ["scenario", "mode", "inject_ts", "detection_time_s", "recovery_time_s", "total", "successful",
          "failed", "recovered", "availability", "p95_latency_ms", "consistency", "consistency_reason"]


def compose(mode, *args, monitor_file=None):
    env = dict(os.environ)
    if monitor_file:
        env["MONITOR_FILE"] = monitor_file
    return subprocess.run(["docker", "compose", "-f", f"docker-compose.{mode}.yml", *args],
                          cwd=ROOT, env=env, check=True, capture_output=True, text=True)


def http(method, path):
    req = urllib.request.Request(f"http://localhost:8080{path}", method=method)
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except Exception:
        return 0


def start_stack(mode, monitor_file):
    for m in ("baseline", "ft"):  # both stacks use ports 8080/8090
        compose(m, "down", "-v")
    path = os.path.join(ROOT, "results", monitor_file)
    if os.path.exists(path):
        os.remove(path)
    compose(mode, "up", "-d", "--build", monitor_file=monitor_file)
    streak = 0
    for _ in range(120):
        ok = all(http("GET", p) == 200 for p in ("/students/1", "/timetable/1", "/transcripts/1"))
        streak = streak + 1 if ok else 0
        if streak >= 3:
            break
        time.sleep(1)
    time.sleep(8)  # let every instance and the replica become healthy


def start_workload(mode, duration, out):
    """Starts the workload container. Returns (process, start time printed by the workload)."""
    p = subprocess.Popen(
        ["docker", "compose", "-f", f"docker-compose.{mode}.yml", "run", "--rm", "-T", "workload",
         "python", "/exp/workload.py", "--duration", str(duration), "--out", out],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        if line.startswith("workload started"):
            return p, float(line.split()[2])
    raise RuntimeError("workload did not start")


def sleep_until(t):
    time.sleep(max(0, t - time.time()))


def run_one(scenario, mode):
    run_id = f"{scenario}_{mode}"
    print(f"\n=== {run_id}", flush=True)
    req_csv = os.path.join(ROOT, "results", "raw", f"{run_id}.csv")
    mon_csv = os.path.join(ROOT, "results", "raw", f"{run_id}_monitor.csv")

    start_stack(mode, f"raw/{run_id}_monitor.csv")
    p, t0 = start_workload(mode, RUN_S, f"/results/raw/{run_id}.csv")
    sleep_until(t0 + BATCH_AT)
    http("POST", "/transcripts/batch")
    sleep_until(t0 + INJECT_AT)
    t_inject = time.time()
    fault = inject(scenario, mode, run_id)
    print(f"injected at t={t_inject - t0:.1f}s", flush=True)

    print(p.communicate()[0].strip().splitlines()[-1], flush=True)  # workload "ALL" summary line
    if fault:
        fault.join()
    time.sleep(5)
    passed, reason = consistency_check.check(mode)

    rows = measure.load_requests(req_csv)
    result = {"scenario": scenario, "mode": mode, "inject_ts": round(t_inject, 3),
              "detection_time_s": measure.detection_time(mon_csv, t_inject),
              "recovery_time_s": measure.recovery_time(rows, t_inject),
              **measure.counts(rows), "p95_latency_ms": measure.p95_latency(req_csv),
              "consistency": "PASS" if passed else "FAIL", "consistency_reason": reason}
    print(result, flush=True)
    compose(mode, "down", "-v")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--modes", default="baseline,ft")
    ap.add_argument("--out", default=os.path.join(ROOT, "results", "experiments.csv"))
    ap.add_argument("--fresh", action="store_true", help="ignore existing results and rerun everything")
    args = ap.parse_args()

    # Resume: keep runs already in the output file, run only the missing ones (--fresh: start over).
    results = []
    if os.path.exists(args.out) and not args.fresh:
        with open(args.out) as f:
            results = list(csv.DictReader(f))
    done = {(r["scenario"], r["mode"]) for r in results}
    for scenario in args.scenarios.split(","):
        for mode in args.modes.split(","):
            if (scenario, mode) in done:
                print(f"skip {scenario}_{mode} (already in {args.out})")
                continue
            results.append(run_one(scenario, mode))
            with open(args.out, "w", newline="") as f:  # rewrite after every run, so partial results survive
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                w.writerows(results)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
