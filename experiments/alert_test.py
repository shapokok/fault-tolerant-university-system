"""Triggers alert rules on purpose and checks which alerts fired.

Per case: fresh stack, workload, fault at t=20 s, alert states recorded by alert_watch.
Writes results/endterm/alert_test.csv (one row per case).
AuthFailureSpike and RateLimitSpike need stage E3, see prometheus/alerts_test.yml.

Note: instance_crash (kill -9) and instance_stop (docker stop) are two different faults.
Usage: python3 experiments/alert_test.py [--cases a,b] [--modes ft,baseline] [--out file]
"""
import argparse
import csv
import os
import time

import measure

from alert_watch import watch_in_background
from inject import ROOT, bank_fault, container, inject, sh
from run_all import compose, sleep_until, start_stack, start_workload

INJECT_AT = 20
RUN_S = 150
FAULT_S = 30


def crash(mode):
    inject("app_crash", mode)  # kill -9 of the app process, same fault as the midterm


def stop_30s(mode):
    name = container(mode, "payment-service-1" if mode == "ft" else "payment-service")
    sh("docker", "stop", name)
    time.sleep(FAULT_S)
    sh("docker", "start", name)


def bank_error(mode):
    bank_fault("error")
    time.sleep(FAULT_S)
    bank_fault("none")


def bank_slow(mode):
    bank_fault("delay", 1)  # below the 2 s timeout: requests are slow, but they do not fail
    time.sleep(45)
    bank_fault("none")


def no_fault(mode):
    pass


# case: (fault, seconds of load, {mode: alerts that must fire}, {mode: alerts that must not fire})
CASES = {
    "instance_crash": (crash, RUN_S, {"ft": {"InstanceRestarted"}, "baseline": {"InstanceDown"}},
                       {"ft": {"InstanceDown"}, "baseline": {"InstanceRestarted"}}),
    "instance_stop": (stop_30s, RUN_S, {"ft": {"InstanceDown"}, "baseline": {"InstanceDown"}}, {}),
    "bank_error": (bank_error, RUN_S, {"ft": {"HighErrorRate", "CircuitBreakerOpen"}, "baseline": {"HighErrorRate"}},
                   {"baseline": {"CircuitBreakerOpen"}}),
    "bank_slow": (bank_slow, RUN_S, {"ft": {"HighLatencyP95"}, "baseline": {"HighLatencyP95"}}, {}),
    "normal_load": (no_fault, 320, {"ft": set(), "baseline": set()},
                    {"ft": {"*"}, "baseline": {"*"}}),  # any alert is a false alert
}
# Alerts that must still be firing at the end, because nothing repairs the fault:
# the baseline has no restart policy, so a killed process stays down.
STAYS_FIRING = {("instance_crash", "baseline"): {"InstanceDown"}}
FIELDS = ["case", "mode", "inject_ts", "alert", "firing_delay_s", "resolved_after_s", "pending_only",
          "requests", "max_poll_gap_s", "run_s", "verdict", "reason"]
RATE = 20          # workload requests per second
MAX_GAP_S = 3      # longest allowed pause between two alert polls


def run_case(case, mode):
    fault, duration, must, must_not = CASES[case]
    must, must_not = must.get(mode, set()), must_not.get(mode, set())
    run = f"{case}_{mode}"
    print(f"\n=== {run}", flush=True)
    start_stack(mode, f"endterm/raw/{run}_monitor.csv")
    p, t0 = start_workload(mode, duration, f"/results/endterm/raw/{run}.csv")
    stop, watcher, rows, stats = watch_in_background(run)
    sleep_until(t0 + INJECT_AT)
    t_inject = time.time()
    fault(mode)
    p.communicate()
    t_end = time.time()
    stop.set()
    watcher.join()
    compose(mode, "down", "-v")

    # Was the run itself valid? (A sleeping laptop once turned 150 s runs into hours.)
    requests = measure.counts(measure.load_requests(os.path.join(ROOT, "results", "endterm", "raw", f"{run}.csv")))["total"]
    run_s = t_end - t0
    invalid = []
    if stats["max_gap_s"] > MAX_GAP_S:
        invalid.append(f"alert polling paused for {stats['max_gap_s']:.0f} s")
    if run_s > duration + 30:
        invalid.append(f"run took {run_s:.0f} s instead of about {duration} s")
    if requests < 0.95 * RATE * duration:
        invalid.append(f"only {requests} of {RATE * duration} requests sent")

    # Keep only what happened after the injection (startup noise is not part of the test).
    rows = [r for r in rows if r["ts"] >= t_inject]
    fired, pending = {}, set()
    for r in rows:
        if r["state"] == "pending":
            pending.add(r["alertname"])
        if r["state"] == "firing":
            fired.setdefault(r["alertname"], r["ts"] - t_inject)
    resolved = {}
    for r in rows:
        if r["state"] == "resolved" and r["alertname"] in fired:
            resolved[r["alertname"]] = r["ts"] - t_inject  # last one wins

    missing = must - set(fired)
    wrong = set(fired) if "*" in must_not else set(fired) & must_not
    stays = STAYS_FIRING.get((case, mode), set())
    never_resolved = set(fired) - set(resolved) - stays
    resolved_anyway = stays & set(resolved)
    problems = [f"did not fire: {sorted(missing)}"] if missing else []
    problems += [f"should not fire: {sorted(wrong)}"] if wrong else []
    problems += [f"did not resolve: {sorted(never_resolved)}"] if never_resolved else []
    problems += [f"resolved although nothing repaired it: {sorted(resolved_anyway)}"] if resolved_anyway else []
    note = f"as expected ({', '.join(sorted(stays))} still firing at the end: nothing restarts the process)" if stays else "as expected"
    verdict = "INVALID" if invalid else "FAIL" if problems else "PASS"
    problems = invalid or problems

    out = []
    for alert in sorted(set(fired) | must) or ["(none)"]:
        out.append({"case": case, "mode": mode, "inject_ts": round(t_inject, 3), "alert": alert,
                    "firing_delay_s": round(fired[alert], 1) if alert in fired else "",
                    "resolved_after_s": round(resolved[alert], 1) if alert in resolved else "",
                    "pending_only": ",".join(sorted(pending - set(fired))), "requests": requests,
                    "max_poll_gap_s": round(stats["max_gap_s"], 1), "run_s": round(run_s), "verdict": verdict,
                    "reason": "; ".join(problems) or note})
    for row in out:
        print(row, flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=",".join(CASES))
    ap.add_argument("--modes", default="ft")
    ap.add_argument("--out", default=os.path.join(ROOT, "results", "endterm", "alert_test.csv"))
    args = ap.parse_args()
    os.makedirs(os.path.join(ROOT, "results", "endterm", "raw"), exist_ok=True)
    results = []
    for mode in args.modes.split(","):
        for case in args.cases.split(","):
            results += run_case(case, mode)
            with open(args.out, "w", newline="") as f:  # rewrite after every case: partial results survive
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                w.writerows(results)


if __name__ == "__main__":
    main()
