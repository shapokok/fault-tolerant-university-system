"""Builds results/endterm/summary.csv and results/endterm/experiments.md from the E5 data.

Only valid runs are used. Numbers come from the CSV files; nothing is typed in by hand.
Usage: python3 experiments/e5_report.py [--dir results/endterm]
"""
import argparse
import collections
import csv
import json
import os
import statistics

import measure
from inject import ROOT

NUMERIC = ["success_rate", "success_rate_passed_limit", "error_rate", "avg_ms", "p95_ms", "detection_s",
           "alert_detection_s", "recovery_s", "attacks_total", "attacks_blocked", "attacks_5xx", "attacks_succeeded"]

TEXT = {
    "instance_failure": dict(
        hypothesis="With a restart policy and a second instance, a crash of one payment instance is hidden from users; without them payments fail until someone restarts the service.",
        configuration="Workload 20 req/s for 75 s; `kill -9` of the payment-service(-1) app process at t=15 s (same fault as the midterm).",
        steps="Fresh stack, warm-up 15 s, crash, 60 s more load, consistency check.",
        expected="FT: about 100% success, recovery 0 s, InstanceRestarted fires, integrity PASS. Baseline: payments fail for the rest of the run, InstanceDown fires.",
        limitations="One crash per run; the restart takes 0.3 to 0.6 s, shorter than one 2 s scrape, so detection depends on timing."),
    "dependency_timeout": dict(
        hypothesis="Timeouts, retries and the circuit breaker keep a slow dependency from slowing every request; the baseline waits for the slow dependency.",
        configuration="Workload 20 req/s for 75 s. Variant bank: mock-bank answers after 5 s for 30 s. Variant db: one session locks the business tables and sleeps (pg_sleep) for 30 s, so every query on them waits (the lock is replayed on the replica too).",
        steps="Fresh stack, warm-up 15 s, slow dependency for 30 s, 30 s more load, consistency check.",
        expected="HighLatencyP95 fires in both modes. FT: shorter waits (2 s timeouts), breaker opens for the bank, money stays consistent. Baseline: requests wait up to the client timeout.",
        limitations="The DB variant locks whole tables; a real slow DB degrades more gradually. FT retries with replica fallback multiply the waiting time (2 s per try per server)."),
    "load_spike": dict(
        hypothesis="The platform serves 50, 150 and 300 users; in the endterm stack the per-IP rate limit caps payments at 20/s from one client and answers the rest with 429 (protection, not failure).",
        configuration="Locust 50 -> 150 -> 300 users, 60 s each, all from one container, so one IP. Each user acts as one student (one login per student, shared). Steps are measured without their first 20 s (ramp up). CPU and memory from docker stats every few seconds.",
        steps="Fresh stack, Locust run, per-request log, consistency check.",
        expected="Baseline: all requests succeed while CPU allows. FT: 429 grows with load on /payments; every request that passes the limit succeeds.",
        limitations="All load comes from one IP, so the per-IP limit acts on the whole test; in reality users have different IPs. Locust shares the laptop CPU with the system under test."),
    "alert_test": dict(
        hypothesis="Each alert rule fires for the fault it is meant for, within seconds, and no rule fires during normal load.",
        configuration="One stack, workload 20 req/s throughout: 5 min normal load, then one fault per rule (full lengths: 30 s faults). FT: kill -9, docker stop 30 s, bank error, bank delay 1 s, 401/403 flood, rate limit flood. Baseline: bank error, bank delay, docker stop, kill -9 (no auth or rate limit there).",
        steps="Segments one after the other; for each, the alerts that start firing inside it are compared with the expected ones.",
        expected="Normal load: 0 alerts. Each segment: its expected alert fires; kill -9 in FT: InstanceRestarted only (not InstanceDown).",
        limitations="1 repetition (agreed); E2 has a second, independent series of the same rules."),
    "unauthorized_access": dict(
        hypothesis="Without a valid token, or with another student's token, nothing can be read or changed, and every attempt is audited.",
        configuration="No background load (so any change can only come from an attack). 80 attempts: no token, forged admin token, expired token, reading another student's record, timetable and transcript, paying for another student, calling an admin endpoint.",
        steps="Checksum of payments and grades, attacks, wait for the alert, checksum again, count audit rows.",
        expected="FT: 80 of 80 refused (401/403), 80 audit rows, checksum unchanged, AuthFailureSpike fires. Baseline: attacks succeed (no authentication).",
        limitations="Attacks come from one client; no slow or distributed brute force."),
    "abusive_requests": dict(
        hypothesis="Malformed input is refused with 4xx (never 5xx) and request floods are cut at the gateway, while normal users keep working.",
        configuration="Normal workload 20 req/s for 75 s (other IP) while this machine sends 500 malformed requests (wrong types, sizes, missing and extra fields, SQL-like strings, broken JSON) and two bursts of 1000 requests (/auth/login, /payments).",
        steps="Fresh stack, warm-up 15 s, attacks, rest of the load, consistency check.",
        expected="FT: 0 attacks answered 5xx, most of the bursts 429, normal users about 100%, integrity PASS, RateLimitSpike fires. Baseline: some malformed input causes 5xx, bursts are fully processed.",
        limitations="The fuzz cases are generated from a fixed seed per run; they are not exhaustive."),
    "recovery_consistency": dict(
        hypothesis="After lost bank answers and a crash during requests, reconciliation leaves no duplicate, lost or pending payment.",
        configuration="Workload 20 req/s for 75 s; mock-bank crash_after_debit for 20 s, then a 1.5 s bank delay and kill -9 of a payment instance while payments are in flight.",
        steps="Fresh stack, warm-up 15 s, faults, rest of the load, reconciliation and consistency check.",
        expected="FT: integrity PASS. Baseline: FAIL (duplicates and money taken without a completed payment), as in the midterm.",
        limitations="The baseline also fails because the workload resends some keys on purpose."),
}


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def mean_std(values):
    values = [v for v in values if v is not None]
    if not values:
        return "", ""
    return round(statistics.mean(values), 4), round(statistics.stdev(values), 4) if len(values) > 1 else ""


def fmt(mean, std, pct=False, unit=""):
    if mean == "":
        return "n/a"
    if pct:
        return f"{mean * 100:.1f}%" + (f" ± {std * 100:.1f}" if std != "" else "")
    return f"{mean:g}{unit}" + (f" ± {std:g}" if std != "" else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "results", "endterm"))
    args = ap.parse_args()
    rows = list(csv.DictReader(open(os.path.join(args.dir, "experiments.csv"))))
    valid = [r for r in rows if r["valid"] == "True"]
    # Detection is recomputed from the raw monitor logs, only inside the load window (fault + 60 s):
    # the first version also counted the stack shutdown after the run (every instance "down").
    fixed = 0
    for r in valid:
        path = os.path.join(args.dir, "raw", "e5", f"{r['run_id']}_monitor.csv")
        if r["detection_s"] != "" and os.path.exists(path):
            t = float(r["inject_ts"])
            new = measure.detection_time(path, t, t + 60)
            fixed += str(new if new is not None else "") != r["detection_s"]
            r["detection_s"] = "" if new is None else str(new)
    invalid = [r for r in rows if r["valid"] != "True"]

    groups = collections.OrderedDict()
    for r in valid:
        groups.setdefault((r["experiment"], r["variant"], r["mode"]), []).append(r)
    summary = []
    for (exp, variant, mode), rs in groups.items():
        s = {"experiment": exp, "variant": variant, "mode": mode, "runs": len(rs)}
        for f in NUMERIC:
            s[f + "_mean"], s[f + "_std"] = mean_std([num(r[f]) for r in rs])
        s["not_recovered_runs"] = sum(1 for r in rs if r["recovery_s"] == "" and exp in ("instance_failure", "dependency_timeout", "recovery_consistency"))
        s["integrity_pass"] = f"{sum(r['integrity'] == 'PASS' for r in rs)}/{len(rs)}"
        s["alerts_ok"] = f"{sum(r['alerts_ok'] == 'True' for r in rs)}/{len(rs)}"
        s["alerts_fired"] = " | ".join(sorted({r["alerts_fired"] for r in rs}))
        summary.append(s)
    with open(os.path.join(args.dir, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0]))
        w.writeheader()
        w.writerows(summary)

    md = ["# E5 experiments", "", "Generated by `experiments/e5_report.py` from `experiments.csv`, `load_spike_steps.csv` and "
          "`alert_test_e5.csv` (do not edit by hand). Values are mean ± standard deviation over the valid runs.",
          f"Detection is recomputed from the raw monitor logs inside the load window ({fixed} run(s) changed: the "
          "recorder had also counted the stack shutdown after the run). n/a = not detected.",
          f"Valid runs: {len(valid)}. Invalid runs (excluded, kept in the CSV): {len(invalid)}"
          + (": " + "; ".join(f"{r['run_id']} ({r['invalid_reason']})" for r in invalid) if invalid else "."), "",
          "Definitions: success = 2xx. `rejected by rate limit` = 429 from the gateway, its own category (not an error). "
          "Error rate = neither 2xx nor 429. Detection = fault to first non-up state in the monitor log. Alert = fault to the "
          "first expected alert firing. Recovery = fault to the start of the first 10 s window with at least 99% success "
          "(n/a = not recovered within the run). Integrity = midterm consistency check (no duplicate, lost or pending "
          "payment) or, for unauthorized_access, payments and grades unchanged.", ""]
    for exp, text in TEXT.items():
        rs = [s for s in summary if s["experiment"] == exp]
        if not rs:
            continue
        md += [f"## {exp}", "", f"**Hypothesis.** {text['hypothesis']}", "", f"**Configuration.** {text['configuration']}", "",
               f"**Steps.** {text['steps']}", "", f"**Expected.** {text['expected']}", "", "**Observed.**", ""]
        if exp == "abusive_requests":
            for s in rs:
                downs = []
                for r in groups[(exp, s["variant"], s["mode"])]:
                    t = float(r["inject_ts"])  # load ends 60 s after the attacks start; later "down" = stack shutdown
                    path = os.path.join(args.dir, "raw", "e5", f"{r['run_id']}_monitor.csv")
                    downs.append(sum(1 for m in csv.DictReader(open(path)) if m["state"] == "down" and t <= float(m["ts"]) < t + 60)
                                 if os.path.exists(path) else None)
                s["monitor_down_during_load"] = ", ".join(map(str, downs))
        if exp in ("unauthorized_access", "abusive_requests"):
            md += ["| Variant | Mode | Runs | Attacks | Refused (4xx) | Answered 5xx | Succeeded (2xx) | Normal users success | Instances down during load (per run) | Integrity | Alerts fired | Expected alerts OK |",
                   "|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for s in rs:
                md.append(f"| {s['variant']} | {s['mode']} | {s['runs']} | {fmt(s['attacks_total_mean'], s['attacks_total_std'])} | "
                          f"{fmt(s['attacks_blocked_mean'], s['attacks_blocked_std'])} | {fmt(s['attacks_5xx_mean'], s['attacks_5xx_std'])} | "
                          f"{fmt(s['attacks_succeeded_mean'], s['attacks_succeeded_std'])} | {fmt(s['success_rate_mean'], s['success_rate_std'], pct=True)} | "
                          f"{s.get('monitor_down_during_load', 'n/a')} | {s['integrity_pass']} | {s['alerts_fired'] or 'none'} | {s['alerts_ok']} |")
        else:
            md += ["| Variant | Mode | Runs | Success | Error rate | Rejected by rate limit | Avg ms | p95 ms | Detection s | Alert s | Recovery s | Integrity | Alerts fired | Expected alerts OK |",
                   "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for s in rs:
                rej = [num(r["rejected_rate_limit"]) for r in groups[(exp, s["variant"], s["mode"])]]
                rec = fmt(s["recovery_s_mean"], s["recovery_s_std"])
                if s["not_recovered_runs"]:
                    rec += f" ({s['not_recovered_runs']} run(s) not recovered)"
                md.append(f"| {s['variant']} | {s['mode']} | {s['runs']} | {fmt(s['success_rate_mean'], s['success_rate_std'], pct=True)} | "
                          f"{fmt(s['error_rate_mean'], s['error_rate_std'], pct=True)} | {fmt(*mean_std(rej))} | "
                          f"{fmt(s['avg_ms_mean'], s['avg_ms_std'])} | {fmt(s['p95_ms_mean'], s['p95_ms_std'])} | "
                          f"{fmt(s['detection_s_mean'], s['detection_s_std'])} | {fmt(s['alert_detection_s_mean'], s['alert_detection_s_std'])} | "
                          f"{rec} | {s['integrity_pass']} | {s['alerts_fired'] or 'none'} | {s['alerts_ok']} |")
        if exp == "load_spike" and os.path.exists(os.path.join(args.dir, "load_spike_steps.csv")):
            steps = collections.defaultdict(list)
            for r in csv.DictReader(open(os.path.join(args.dir, "load_spike_steps.csv"))):
                if any(r["run_id"] == v["run_id"] for v in valid):
                    steps[(r["mode"], int(r["users"]))].append(r)
            md += ["", "Per step (steady part of each 60 s step):", "",
                   "| Mode | Users | Runs | Success of all requests | Success of requests that passed the limit | Rejected by rate limit | 5xx | Throughput ok req/s | p50 ok ms | p95 ok ms | CPU % (all containers) | Memory MiB |",
                   "|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for (mode, users), rs2 in sorted(steps.items()):
                g = lambda f: mean_std([num(r[f]) for r in rs2])  # noqa: E731
                md.append(f"| {mode} | {users} | {len(rs2)} | {fmt(*g('success_rate_all'), pct=True)} | {fmt(*g('success_rate_passed_limit'), pct=True)} | "
                          f"{fmt(*g('rejected_rate_limit'))} | {fmt(*g('errors_5xx'))} | {fmt(*g('throughput_ok_rps'))} | {fmt(*g('p50_ok_ms'))} | "
                          f"{fmt(*g('p95_ok_ms'))} | {fmt(*g('cpu_total_percent'))} | {fmt(*g('mem_total_mib'))} |")
            md += ["", "All load comes from one IP (one Locust container). In the endterm stack the per-IP limit of 20 payment "
                   "requests/s therefore applies to the whole test: the 429 answers are the protection working, not a failure. "
                   "That is why two success rates are given: of all requests, and of the requests that passed the limit."]
        if exp == "alert_test" and os.path.exists(os.path.join(args.dir, "alert_test_e5.csv")):
            md += ["", "Per segment:", "", "| Mode | Segment | Expected | Fired | First expected alert after s | Verdict |", "|---|---|---|---|---|---|"]
            for r in csv.DictReader(open(os.path.join(args.dir, "alert_test_e5.csv"))):
                if any(r["run_id"] == v["run_id"] for v in valid):
                    md.append(f"| {r['mode']} | {r['segment']} | {r['expected'] or 'none'} | {r['fired'] or 'none'} | {r['first_expected_after_s'] or 'n/a'} | {r['verdict']} |")
        n = sorted({s["runs"] for s in rs})
        md += ["", f"**Repetitions.** {', '.join(map(str, n))} valid run(s) per mode.", "", f"**Limitations.** {text['limitations']}", ""]
    with open(os.path.join(args.dir, "experiments.md"), "w") as f:
        f.write("\n".join(md) + "\n")
    print(f"{len(valid)} valid, {len(invalid)} invalid runs; wrote summary.csv and experiments.md in {args.dir}")


if __name__ == "__main__":
    main()
