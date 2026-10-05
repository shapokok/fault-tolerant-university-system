"""Stage 5 analysis. Reads results/ and writes tables + plots.

Run from the project root: python3 analysis/metrics.py
Outputs:
  results/before_after.csv, results/before_after.md   baseline vs ft per scenario
  results/soak_metrics.csv                              observed MTTF, MTBF, MTTR, lambda, A
  results/rbd.csv                                       theoretical (RBD) vs observed availability
  results/plots/*.png                                   success rate over time
"""
import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "experiments"))
import measure  # noqa: E402  (same metric definitions as the experiments)

RES = os.path.join(ROOT, "results")
PLOTS = os.path.join(RES, "plots")
FAULT_S = 30
WINDOW = {"app_crash": 0, "high_load": 60}  # fault length on the plot; default FAULT_S
COLORS = {"baseline": "#d1495b", "ft": "#2e86ab"}
MIX = {"timetable": 0.4, "student": 0.2, "payment": 0.2, "registration": 0.1, "transcript": 0.1}


def md_table(df):
    lines = ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |" for row in df.values]
    return "\n".join(lines) + "\n"


def success_series(path, t0, bin_s=1.0):
    df = pd.read_csv(path)
    df["ok"] = df["status"].between(200, 299)
    df["t"] = ((df["ts"] - t0) // bin_s) * bin_s
    return df.groupby("t")["ok"].mean() * 100


# ---------- 1. before / after table + per-scenario plots ----------

def before_after():
    exp = pd.read_csv(os.path.join(RES, "experiments.csv"))
    rows = []
    for scenario, g in exp.groupby("scenario", sort=False):
        r = {"scenario": scenario}
        for _, x in g.iterrows():
            m = x["mode"]
            r[f"availability_{m}"] = f"{x['availability'] * 100:.2f}%"
            r[f"detection_s_{m}"] = "not detected" if pd.isna(x["detection_time_s"]) else x["detection_time_s"]
            r[f"recovery_s_{m}"] = "not recovered" if pd.isna(x["recovery_time_s"]) else x["recovery_time_s"]
            r[f"failed_{m}"] = int(x["failed"])
            r[f"recovered_{m}"] = int(x["recovered"])
            r[f"consistency_{m}"] = x["consistency"]
        rows.append(r)
    table = pd.DataFrame(rows)
    table.to_csv(os.path.join(RES, "before_after.csv"), index=False)
    with open(os.path.join(RES, "before_after.md"), "w") as f:
        f.write(md_table(table))
        f.write("\nConsistency reasons:\n\n")
        for _, x in exp.iterrows():
            f.write(f"- {x['scenario']} / {x['mode']}: {x['consistency']}, {x['consistency_reason']}\n")
    print(md_table(table))

    for scenario, g in exp.groupby("scenario", sort=False):
        fig, ax = plt.subplots(figsize=(9, 3.6))
        for _, x in g.iterrows():
            path = os.path.join(RES, "raw", f"{scenario}_{x['mode']}.csv")
            s = success_series(path, x["inject_ts"])
            ax.plot(s.index, s.values, color=COLORS[x["mode"]], lw=1.4,
                    label=f"{x['mode']} (availability {x['availability'] * 100:.1f}%)")
        ax.axvline(0, color="black", ls="--", lw=1)
        ax.text(0.5, 3, " injection", fontsize=9)
        w = WINDOW.get(scenario, FAULT_S)
        if w:
            ax.axvspan(0, w, color="grey", alpha=0.12, label=f"fault active ({w} s)")
        ax.set(title=f"{scenario}: success rate over time (1 s bins)", xlabel="seconds since injection",
               ylabel="successful requests, %", ylim=(0, 105))
        ax.legend(loc="lower left", fontsize=8)
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(PLOTS, f"{scenario}.png"), dpi=120)
        plt.close(fig)

    # availability bar chart
    fig, ax = plt.subplots(figsize=(9, 3.6))
    scenarios = list(dict.fromkeys(exp["scenario"]))
    for i, mode in enumerate(["baseline", "ft"]):
        vals = [exp[(exp.scenario == s) & (exp["mode"] == mode)]["availability"].mean() * 100 for s in scenarios]
        ax.bar([k + (i - 0.5) * 0.38 for k in range(len(scenarios))], vals, 0.38, color=COLORS[mode], label=mode)
    ax.set_xticks(range(len(scenarios)), scenarios)
    ax.set(ylabel="availability, %", title="Availability per scenario (90 s runs)")
    ax.set_ylim(min(50, ax.get_ylim()[0]), 100.5)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1))
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, "availability.png"), dpi=120)
    plt.close(fig)


# ---------- 2. observed MTTF / MTBF / MTTR from the soak runs ----------

def load_events(mode):
    with open(os.path.join(RES, f"soak_{mode}_events.csv")) as f:
        return [(float(r["ts"]), r["scenario"]) for r in csv.DictReader(f)]


def soak_metrics():
    out = []
    for mode in ("baseline", "ft"):
        rows = measure.load_requests(os.path.join(RES, f"soak_{mode}_requests.csv"))
        events = load_events(mode)
        t_start, t_end = rows[0][0], rows[-1][0]
        T = t_end - t_start
        # Down interval per injected fault: injection -> recovery (same definition as the experiments).
        # Not recovered before the next fault -> still down when it hits, so the intervals touch.
        intervals = []
        for i, (ts, _) in enumerate(events):
            until = events[i + 1][0] if i + 1 < len(events) else t_end
            ttr = measure.recovery_time(rows, ts, t_until=until)
            if ttr is None:
                ttr = until - ts
            if ttr > 0:                  # users saw errors
                intervals.append([ts, ts + ttr])
        merged = []                      # a fault that hits an already-down system is not a new failure
        for a, b in intervals:
            if merged and a <= merged[-1][1] + 0.5:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        failures = len(merged)
        downtime = sum(b - a for a, b in merged)
        unrecovered = int(bool(merged) and merged[-1][1] >= t_end - 10.5)  # still down at the end
        req = measure.counts(rows)
        r = {"mode": mode, "duration_s": round(T), "injected_faults": len(events),
             "component_mtbf_s": round(T / len(events), 1),
             "system_failures": failures, "unrecovered": unrecovered}
        if failures:
            mttr = downtime / failures
            mttf = (T - downtime) / failures
            r.update(mttf_s=round(mttf, 1), mttr_s=round(mttr, 1), mtbf_s=round(mttf + mttr, 1),
                     failure_rate_per_h=round(3600 / mttf, 2), availability_time=round(mttf / (mttf + mttr), 4))
        else:  # every fault was masked: MTTF is at least the whole run
            r.update(mttf_s=f">{round(T)}", mttr_s=0, mtbf_s=f">{round(T)}",
                     failure_rate_per_h=f"<{round(3600 / T, 2)}", availability_time=1.0)
        r["availability_requests"] = req["availability"]
        out.append(r)

        # soak plot
        fig, ax = plt.subplots(figsize=(11, 3.4))
        s = success_series(os.path.join(RES, f"soak_{mode}_requests.csv"), t_start, bin_s=5)
        ax.plot(s.index / 60, s.values, color=COLORS[mode], lw=1)
        for ts, name in events:
            ax.axvline((ts - t_start) / 60, color="black", ls="--", lw=0.8)
            ax.text((ts - t_start) / 60, 2, " " + name, rotation=90, fontsize=7, va="bottom")
        ax.set(title=f"Soak {mode}: success rate (5 s bins), dashed = injected failure",
               xlabel="minutes", ylabel="successful requests, %", ylim=(0, 105))
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(PLOTS, f"soak_{mode}.png"), dpi=120)
        plt.close(fig)

    pd.DataFrame(out).to_csv(os.path.join(RES, "soak_metrics.csv"), index=False)
    print(md_table(pd.DataFrame(out)))
    return out


# ---------- 3. theoretical availability from a reliability block diagram ----------

def series(*a):
    p = 1.0
    for x in a:
        p *= x
    return p


def parallel(*a):
    q = 1.0
    for x in a:
        q *= 1 - x
    return 1 - q


def component_downtime(mode):
    """Down seconds per component during the soak, from the injection schedule.
    Reverted faults last FAULT_S. A crashed process is down until the monitor sees it up again."""
    events = load_events(mode)
    rows = measure.load_requests(os.path.join(RES, f"soak_{mode}_requests.csv"))
    t_end = rows[-1][0]
    T = t_end - rows[0][0]
    mon = pd.read_csv(os.path.join(RES, f"soak_{mode}_monitor.csv"))
    crash_target = "payment-1" if mode == "ft" else "payment"
    intervals = {c: [] for c in ("node_b", "payment_proc", "db_primary", "bank")}
    count = {c: 0 for c in intervals}
    for ts, scenario in events:
        if scenario == "app_crash":
            ups = mon[(mon.target == crash_target) & (mon.state == "up") & (mon.ts > ts)]
            d = (ups.ts.min() - ts) if len(ups) else (t_end - ts)  # baseline: never restarted
            comp = "payment_proc"
        else:
            d = FAULT_S
            comp = {"db_failure": "db_primary", "node_failure": "node_b",
                    "network_timeout": "bank", "lost_transaction": "bank"}[scenario]
        intervals[comp].append((ts, min(ts + d, t_end)))
        count[comp] += 1
    down = {}
    for c, iv in intervals.items():  # union of intervals: a dead process cannot be down twice
        total, last_end = 0.0, None
        for a, b in sorted(iv):
            if last_end is not None and a < last_end:
                a = last_end
            if b > a:
                total += b - a
                last_end = b
        down[c] = total
    A = {c: 1 - down[c] / T for c in down}
    return A, down, count, T


def rbd():
    out = []
    obs = {r["mode"]: r for r in csv.DictReader(open(os.path.join(RES, "soak_metrics.csv")))}
    for mode in ("baseline", "ft"):
        A, down, count, T = component_downtime(mode)
        if mode == "baseline":
            # One node holds every service and the DB: everything is in series.
            node = A["node_b"]
            path = {
                "timetable": series(node, A["db_primary"]),
                "student": series(node, A["db_primary"]),
                "registration": series(node, A["db_primary"]),
                "transcript": series(node, A["db_primary"]),
                "payment": series(node, A["payment_proc"], A["db_primary"], A["bank"]),
            }
        else:
            # Instance 1 on node-a (not failed in tests), instance 2 on node-b: in parallel.
            svc = parallel(1.0, A["node_b"])                       # student / transcript
            pay = parallel(A["payment_proc"], A["node_b"])         # payment-1 crashes, payment-2 on node-b
            db_read = parallel(A["db_primary"], A["node_b"])        # replica lives on node-b
            db_write = A["db_primary"]                              # replica is read-only without failover
            path = {
                "timetable": series(svc, db_read),
                "student": series(svc, db_read),
                "registration": series(svc, db_write),
                "transcript": series(svc, svc, db_read),
                "payment": series(pay, db_write, A["bank"]),
            }
        a_theory = sum(MIX[k] * path[k] for k in MIX)
        r = {"mode": mode, **{f"A_{c}": round(v, 4) for c, v in A.items()},
             **{f"faults_{c}": count[c] for c in count},
             **{f"A_path_{k}": round(v, 4) for k, v in path.items()},
             "A_theoretical": round(a_theory, 4),
             "A_observed_requests": float(obs[mode]["availability_requests"]),
             "A_observed_time": float(obs[mode]["availability_time"])}
        out.append(r)
    pd.DataFrame(out).to_csv(os.path.join(RES, "rbd.csv"), index=False)
    print(md_table(pd.DataFrame(out).T.reset_index().rename(columns={"index": "metric", 0: "baseline", 1: "ft"})))


if __name__ == "__main__":
    os.makedirs(PLOTS, exist_ok=True)
    before_after()
    if os.path.exists(os.path.join(RES, "soak_ft_events.csv")):
        soak_metrics()
        rbd()
    else:
        print("soak results not found yet, skipping MTTF/MTTR and RBD")
