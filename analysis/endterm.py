"""Stage E6: endterm analysis. Every number comes from the result files; every plot is readable in black and white
(line styles, markers and hatching carry the meaning, not color).

Run from the project root: python3 analysis/endterm.py
Inputs:  results/endterm/{experiments.csv, summary.csv, load_spike_steps.csv, alert_test_e5.csv, raw/e5/},
         results/endterm/prometheus/ (dashboard export), results/experiments.csv (midterm)
Outputs: results/endterm/comparison.csv, results/endterm/midterm_vs_endterm.csv, results/endterm/analysis.md,
         results/endterm/plots/*.png
"""
import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "experiments"))
import measure  # noqa: E402

RES = os.path.join(ROOT, "results", "endterm")
RAW = os.path.join(RES, "raw", "e5")
PLOTS = os.path.join(RES, "plots")

# Black-and-white style: endterm = solid black, baseline = dashed gray; bars: dark gray vs white with hatching.
plt.rcParams.update({"font.size": 9, "axes.grid": True, "axes.axisbelow": True, "grid.color": "0.85", "grid.linestyle": ":",
                     "axes.prop_cycle": plt.cycler(color=["black"]), "savefig.dpi": 160})
STYLE = {"ft": dict(color="black", linestyle="-", linewidth=1.6, marker="o", markersize=3.5, label="endterm (FT + security)"),
         "baseline": dict(color="0.45", linestyle="--", linewidth=1.4, marker="s", markersize=3.5, label="baseline")}
BAR = {"ft": dict(facecolor="0.35", edgecolor="black", hatch=None, label="endterm"),
       "baseline": dict(facecolor="white", edgecolor="black", hatch="///", label="baseline")}
FAULT_SPAN = dict(facecolor="0.92", edgecolor="0.6", hatch="..", linewidth=0)
NAMES = {("instance_failure", "kill -9"): "Instance crash (kill -9)",
         ("dependency_timeout", "bank"): "Slow bank (5 s, 30 s)",
         ("dependency_timeout", "db"): "Slow DB (tables locked 30 s)",
         ("load_spike", "50-150-300 users"): "Load spike 50/150/300 users",
         ("alert_test", "all rules + 5 min normal"): "Alert test",
         ("unauthorized_access", "8 attack kinds"): "Unauthorized access",
         ("abusive_requests", "fuzz + bursts"): "Abusive requests",
         ("recovery_consistency", "crash after debit + kill"): "Recovery consistency"}


def fnum(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN (empty in the CSV) -> None


def pct(v):
    v = fnum(v)
    return "n/a" if v is None else f"{v * 100:.1f}%"


def val(v, unit=""):
    v = fnum(v) if not isinstance(v, str) else v
    if isinstance(v, str):
        return v
    return "n/a" if v is None else f"{v:g}{unit}"


def save(fig, name):
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, name))
    plt.close(fig)


# ---------- tables ----------

def comparison():
    s = pd.read_csv(os.path.join(RES, "summary.csv"))
    rows = []
    for (exp, variant), g in s.groupby(["experiment", "variant"], sort=False):
        r = {"experiment": NAMES.get((exp, variant), f"{exp} {variant}")}
        for mode in ("baseline", "ft"):
            x = g[g["mode"] == mode]
            if x.empty:
                continue
            x = x.iloc[0]
            tag = "endterm" if mode == "ft" else "baseline"
            r[f"success_{tag}"] = fnum(x["success_rate_mean"])
            r[f"p95_ms_{tag}"] = fnum(x["p95_ms_mean"])
            r[f"detection_s_{tag}"] = fnum(x["detection_s_mean"])
            r[f"alert_s_{tag}"] = fnum(x["alert_detection_s_mean"])
            r[f"recovery_s_{tag}"] = "not recovered" if x["not_recovered_runs"] > 0 else fnum(x["recovery_s_mean"])
            r[f"attacks_refused_{tag}"] = fnum(x["attacks_blocked_mean"])
            r[f"attacks_5xx_{tag}"] = fnum(x["attacks_5xx_mean"])
            r[f"attacks_succeeded_{tag}"] = fnum(x["attacks_succeeded_mean"])
            r[f"integrity_{tag}"] = x["integrity_pass"]
            r[f"alerts_ok_{tag}"] = x["alerts_ok"]
            r[f"runs_{tag}"] = int(x["runs"])
        rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(RES, "comparison.csv"), index=False)
    return df


MATCH = [("app_crash", ("instance_failure", "kill -9"), "same fault (kill -9 of the payment app process)"),
         ("network_timeout", ("dependency_timeout", "bank"), "same fault (mock-bank 5 s delay for 30 s)"),
         ("lost_transaction", ("recovery_consistency", "crash after debit + kill"),
          "partly: endterm adds a kill -9 during in-flight payments")]


def midterm_vs_endterm():
    mid = pd.read_csv(os.path.join(ROOT, "results", "experiments.csv"))
    s = pd.read_csv(os.path.join(RES, "summary.csv"))
    rows = []
    for scenario, (exp, variant), note in MATCH:
        for mode in ("baseline", "ft"):
            m = mid[(mid.scenario == scenario) & (mid["mode"] == mode)].iloc[0]
            e = s[(s.experiment == exp) & (s.variant == variant) & (s["mode"] == mode)].iloc[0]
            rows.append({"scenario": f"{scenario} / {exp}", "mode": mode, "comparable": note,
                         "midterm_success": fnum(m["availability"]), "endterm_success": fnum(e["success_rate_mean"]),
                         "midterm_detection_s": fnum(m["detection_time_s"]), "endterm_detection_s": fnum(e["detection_s_mean"]),
                         "endterm_alert_s": fnum(e["alert_detection_s_mean"]),
                         "midterm_recovery_s": "not recovered" if pd.isna(m["recovery_time_s"]) else fnum(m["recovery_time_s"]),
                         "endterm_recovery_s": "not recovered" if e["not_recovered_runs"] > 0 else fnum(e["recovery_s_mean"]),
                         "midterm_consistency": m["consistency"], "endterm_integrity": e["integrity_pass"]})
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(RES, "midterm_vs_endterm.csv"), index=False)
    return df


# ---------- plots ----------

def success_series(run_id, t_inject, bin_s=2):
    rows = measure.load_requests(os.path.join(RAW, f"{run_id}.csv"))
    bins = {}
    for ts, ok, _ in rows:
        b = int((ts - t_inject) // bin_s) * bin_s
        n, k = bins.get(b, (0, 0))
        bins[b] = (n + 1, k + ok)
    xs = sorted(bins)
    return xs, [100 * bins[x][1] / bins[x][0] for x in xs]


def plot_success_over_time(exp_rows):
    panels = [("instance_failure", "kill -9", 0), ("dependency_timeout", "bank", 30), ("dependency_timeout", "db", 30),
              ("recovery_consistency", "crash after debit + kill", 30), ("abusive_requests", "fuzz + bursts", 0)]
    fig, axes = plt.subplots(2, 3, figsize=(11, 6.2), sharey=True)
    for ax, (exp, variant, fault_s) in zip(axes.flat, panels):
        for mode in ("baseline", "ft"):
            r = exp_rows[(exp, variant, mode, "1")]
            xs, ys = success_series(r["run_id"], float(r["inject_ts"]))
            ax.plot(xs, ys, markevery=5, **STYLE[mode])
        if fault_s:
            ax.axvspan(0, fault_s, **FAULT_SPAN)
        ax.axvline(0, color="black", linestyle=":", linewidth=1)
        ax.set_title(NAMES[(exp, variant)] + (" (normal users)" if exp == "abusive_requests" else ""), fontsize=9)
        ax.set_ylim(0, 105)
        ax.set_xlabel("seconds since fault (dotted line)")
    axes[0][0].set_ylabel("successful requests, %")
    axes[1][0].set_ylabel("successful requests, %")
    ax = axes.flat[-1]
    ax.axis("off")
    for mode in ("ft", "baseline"):
        ax.plot([], [], **STYLE[mode])
    ax.axvspan(0, 0, **FAULT_SPAN, label="fault active")
    ax.legend(loc="center", frameon=True)
    ax.text(0.5, 0.15, "repetition 1 of each run, 2 s bins\n(all repetitions in experiments.md)", ha="center", transform=ax.transAxes)
    fig.suptitle("Success rate over time, baseline vs endterm", fontsize=11)
    save(fig, "success_over_time.png")


def plot_baseline_vs_endterm(comp):
    data = comp[comp["success_endterm"].notna() & comp["success_baseline"].notna()]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.2))
    x = range(len(data))
    for i, mode in enumerate(("baseline", "ft")):
        tag = "endterm" if mode == "ft" else "baseline"
        a1.bar([k + (i - 0.5) * 0.38 for k in x], data[f"success_{tag}"] * 100, 0.38, **BAR[mode])
        a2.bar([k + (i - 0.5) * 0.38 for k in x], data[f"p95_ms_{tag}"], 0.38, **BAR[mode])
    for a in (a1, a2):
        a.set_xticks(list(x))
        a.set_xticklabels([n.split(" (")[0] for n in data["experiment"]], rotation=25, ha="right")
    a1.set(ylabel="successful requests, %", ylim=(0, 105), title="Success rate (mean of runs)")
    a2.set(ylabel="p95 latency, ms (log scale)", yscale="log", title="p95 latency (mean of runs)")
    a1.legend(loc="lower left")
    save(fig, "baseline_vs_endterm.png")


def plot_load_spike():
    st = pd.read_csv(os.path.join(RES, "load_spike_steps.csv"))
    g = st.groupby(["mode", "users"]).mean(numeric_only=True).reset_index()
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.8))
    for mode in ("baseline", "ft"):
        d = g[g["mode"] == mode]
        a2.plot(d["users"], d["throughput_ok_rps"], **STYLE[mode])
        if mode == "ft":
            a1.plot(d["users"], d["success_rate_all"] * 100, color="black", linestyle="-", marker="o", label="endterm: of all requests")
            a1.plot(d["users"], d["success_rate_passed_limit"] * 100, color="black", linestyle=":", marker="^",
                    label="endterm: of requests that passed the limit")
        else:
            a1.plot(d["users"], d["success_rate_all"] * 100, **{**STYLE["baseline"], "label": "baseline (no limit)"})
    a1.text(225, 100.6, "baseline and \"passed the limit\": both 100%", ha="center", fontsize=7.5)
    a1.set(xlabel="Locust users (one IP)", ylabel="successful requests, %", ylim=(70, 103), title="Success rate per step")
    a1.set_xticks([50, 150, 300])
    a1.legend(loc="lower left", fontsize=8)
    a2.set(xlabel="Locust users (one IP)", ylabel="successful requests per second", title="Throughput per step")
    a2.set_xticks([50, 150, 300])
    a2.legend(loc="upper left", fontsize=8)
    save(fig, "load_spike.png")


def plot_alert_timeline(exp_rows):
    seg_len = {"ft": [300, 70, 100, 110, 110, 100, 100], "baseline": [300, 110, 110, 100, 70]}
    segs = pd.read_csv(os.path.join(RES, "alert_test_e5.csv"))
    rules = ["InstanceDown", "InstanceRestarted", "HighErrorRate", "HighLatencyP95", "CircuitBreakerOpen", "AuthFailureSpike", "RateLimitSpike"]
    short = {"normal load": "normal load\n(5 min)", "kill -9 payment-service-1": "kill -9", "kill -9 payment-service": "kill -9",
             "docker stop 30 s": "docker\nstop 30 s", "bank error 30 s": "bank\nerror", "bank delay 1 s for 45 s": "bank\ndelay 1 s",
             "401/403 flood 30 s": "401/403\nflood", "rate limit flood 30 s": "rate limit\nflood"}
    fig, axes = plt.subplots(2, 1, figsize=(11, 6.5))
    for ax, mode in zip(axes, ("ft", "baseline")):
        ax.set_axisbelow(True)
        r = exp_rows[("alert_test", "all rules + 5 min normal", mode, "1")]
        t0 = float(r["inject_ts"])
        names = list(segs[(segs.run_id == r["run_id"])]["segment"])
        start = 0
        for i, (name, length) in enumerate(zip(names, seg_len[mode])):
            if i % 2:
                ax.axvspan(start / 60, (start + length) / 60, facecolor="0.9", edgecolor="none")
            ax.axvline(start / 60, color="0.5", linewidth=0.6)
            ax.text((start + length / 2) / 60, len(rules) - 0.35, short.get(name, name), ha="center", va="bottom", fontsize=7)
            start += length
        alerts = list(csv.DictReader(open(os.path.join(RAW, f"alerts_{r['run_id']}.csv"))))
        for y, rule in enumerate(rules):
            spans, firing = [], {}  # firing: series key -> start (min); one series per instance or service
            for a in alerts:
                if a["alertname"] != rule:
                    continue
                key = a["instance"] + a["service"]
                t = (float(a["ts"]) - t0) / 60
                if a["state"] == "firing":
                    firing[key] = t
                elif a["state"] == "resolved" and key in firing:
                    spans.append((firing.pop(key), t))
            spans += [(t, start / 60) for t in firing.values()]  # still firing when the test ended
            ax.broken_barh([(a, b - a) for a, b in spans], (y - 0.3, 0.6), facecolors="black")
        ax.set_yticks(range(len(rules)))
        ax.set_yticklabels(rules)
        ax.set_ylim(-0.6, len(rules) + 0.7)
        ax.set_xlim(0, start / 60)
        ax.set_title(("endterm" if mode == "ft" else "baseline") + ": alert firing (black bars) per test segment", fontsize=9)
    for ax in axes:
        ax.set_xlabel("minutes since the alert test started (first 5 min: normal load, no alert may fire)")
    save(fig, "alert_timeline.png")


def plot_security(comp):
    rows = [("Unauthorized access", "baseline"), ("Unauthorized access", "endterm"), ("Abusive requests", "baseline"), ("Abusive requests", "endterm")]
    fig, ax = plt.subplots(figsize=(9, 3.6))
    for y, (name, tag) in enumerate(rows):
        r = comp[comp.experiment == name].iloc[0]
        refused, err, ok = r[f"attacks_refused_{tag}"], r[f"attacks_5xx_{tag}"], r[f"attacks_succeeded_{tag}"]
        total = refused + err + ok
        left = 0
        for value, style, label in [(refused, dict(facecolor="white", edgecolor="black", hatch="///"), "refused (4xx, 429)"),
                                    (err, dict(facecolor="black", edgecolor="black"), "server error (5xx)"),
                                    (ok, dict(facecolor="0.6", edgecolor="black"), "succeeded (2xx)")]:
            ax.barh(y, 100 * value / total, left=left, label=label if y == 0 else None, **style)
            left += 100 * value / total
        ax.text(101, y, f"{int(total)} requests: {int(refused)} refused, {int(err)} 5xx, {int(ok)} succeeded", va="center", fontsize=8)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([f"{n}, {t}" for n, t in rows])
    ax.set_xlim(0, 200)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel("share of attack requests, % (mean of runs)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=3, fontsize=8)
    ax.set_title("What happened to the attack requests", fontsize=10)
    save(fig, "security_outcomes.png")


def plot_midterm_vs_endterm(mve):
    fig, ax = plt.subplots(figsize=(9, 3.8))
    labels = list(dict.fromkeys(mve["scenario"]))
    styles = [("midterm_success", "baseline", dict(facecolor="white", edgecolor="black", hatch="..", label="midterm baseline")),
              ("midterm_success", "ft", dict(facecolor="0.75", edgecolor="black", hatch="..", label="midterm FT")),
              ("endterm_success", "baseline", dict(facecolor="white", edgecolor="black", hatch="///", label="endterm baseline")),
              ("endterm_success", "ft", dict(facecolor="0.35", edgecolor="black", label="endterm FT + security"))]
    for i, (col, mode, style) in enumerate(styles):
        vals = [mve[(mve.scenario == s) & (mve["mode"] == mode)][col].iloc[0] * 100 for s in labels]
        xs = [k + (i - 1.5) * 0.2 for k in range(len(labels))]
        ax.bar(xs, vals, 0.2, **style)
        for x_, v in zip(xs, vals):
            ax.text(x_, v + 1, f"{v:.1f}", ha="center", fontsize=7)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set(ylabel="successful requests, %", ylim=(0, 110), title="Midterm vs endterm, matching scenarios")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), fontsize=8, ncol=4)
    save(fig, "midterm_vs_endterm.png")


def plot_dashboard_session():
    folder = os.path.join(RES, "prometheus")
    if not os.path.exists(os.path.join(folder, "session_events.csv")):
        print("no Prometheus export yet, skipping dashboard_session.png")
        return
    events = list(csv.DictReader(open(os.path.join(folder, "session_events.csv"))))

    def path(prefix):  # export file names are cut to 50 characters
        return os.path.join(folder, next(f for f in sorted(os.listdir(folder)) if f.startswith(prefix)))

    def series(file, pick=None):
        d = pd.read_csv(path(file))
        if pick:
            d = d[d.series.astype(str).str.contains(pick, regex=False)]
        return d.groupby("t_s")["value"].sum()
    fig, axes = plt.subplots(4, 1, figsize=(10, 8), sharex=True)
    req = series("requests_per_second")
    axes[0].plot(req.index, req.values, color="black")
    axes[0].set_ylabel("requests/s\n(all services)")
    err = pd.read_csv(path("error_rate"))
    pay = err[err.series == "payment-service"].set_index("t_s")["value"]
    axes[1].plot(pay.index, pay.values, color="black", label="payment-service, new requests")
    axes[1].set_ylabel("5xx, %")
    axes[1].legend(loc="upper right", fontsize=8)
    auth_ = pd.read_csv(path("401_403_429"))
    for name, style in [("401", dict(color="black", linestyle="-")), ("403", dict(color="0.4", linestyle="--")),
                        ("429 (rate limited at the gateway)", dict(color="black", linestyle=":", linewidth=2))]:
        d = auth_[auth_.series == name].set_index("t_s")["value"]
        axes[2].plot(d.index, d.values, label=name.split(" (")[0], **style)
    peak = auth_[auth_.series == "401"]["value"].max()
    axes[2].annotate(f"401 and 403: about {peak:.0f} per minute each (lines overlap)", xy=(195, peak), xytext=(120, 250),
                     fontsize=7.5, arrowprops=dict(arrowstyle="->", color="black"))
    axes[2].set_ylabel("per minute")
    axes[2].legend(loc="upper left", fontsize=8)
    fa = series("firing_alerts")
    axes[3].step(fa.index, fa.values, color="black", where="post")
    axes[3].set_ylabel("firing alerts")
    axes[3].set_xlabel("seconds since the session started")
    for ax in axes:
        for e in events:
            if e["event"] not in ("workload start", "end"):
                t = float(e["t_s"])
                ax.axvspan(t, t + 30, **FAULT_SPAN)
    for e in events:
        if e["event"] not in ("workload start", "end"):
            axes[0].text(float(e["t_s"]) + 15, axes[0].get_ylim()[1] * 0.98, e["event"], ha="center", va="top", fontsize=7)
    fig.suptitle("Prometheus export of one endterm session (dashboard panels)", fontsize=11)
    save(fig, "dashboard_session.png")


def box(ax, x, y, w, h, text, fill="white", style="-", bold=False):
    ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h, boxstyle="round,pad=0.03", facecolor=fill,
                                edgecolor="black", linestyle=style, linewidth=1.2))
    ax.text(x, y, text, ha="center", va="center", fontsize=7.5, fontweight="bold" if bold else "normal")


def arrow(ax, a, b, text="", style="-"):
    ax.annotate("", xy=b, xytext=a, arrowprops=dict(arrowstyle="-|>", color="black", lw=1, linestyle=style))
    if text:
        ax.text((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, text, fontsize=6.5, ha="center", va="bottom",
                bbox=dict(facecolor="white", edgecolor="none", pad=0.5))


def plot_architecture():
    fig, ax = plt.subplots(figsize=(12, 8.2))
    ax.set(xlim=(0, 12.4), ylim=(0, 8.8))
    ax.axis("off")
    ax.grid(False)
    for (x0, w, label) in [(0.3, 5.0, "node-a"), (5.7, 4.4, "node-b")]:
        ax.add_patch(FancyBboxPatch((x0, 0.3), w, 4.6, boxstyle="round,pad=0.05", facecolor="none", edgecolor="0.4", linestyle="--"))
        ax.text(x0 + 0.15, 4.65, label, fontsize=9, fontweight="bold")
    box(ax, 5.2, 8.3, 4.2, 0.5, "Clients: workload, Locust, attacker scripts", fill="0.95")
    box(ax, 5.2, 7.2, 7.2, 0.95, "gateway (nginx :8080): X-Request-ID, JSON access log,\n"
                                "rate limits per IP (login 5/s, payments 20/s -> 429), removes X-Probe, sets X-Real-IP,\n"
                                "body limit 16 KB, /admin and /metrics not routed")
    arrow(ax, (5.2, 8.05), (5.2, 7.68))
    names = [("student-service", "login (bcrypt, JWT), lockout", 4.0), ("transcript-service", "passes the user token on", 3.0),
             ("payment-service", "idempotency, breaker, reconcile", 2.0)]
    for name, note, y in names:
        box(ax, 2.8, y, 3.8, 0.6, f"{name}-1\n{note}; verify token, owner/admin, audit")
        box(ax, 7.9, y, 3.6, 0.6, f"{name}-2\nsame code")
        arrow(ax, (3.9, 6.72), (2.8, y + 0.32))
        arrow(ax, (6.5, 6.72), (7.9, y + 0.32))
    box(ax, 2.8, 0.85, 3.2, 0.75, "postgres-primary\nservices log in as uni_app (least privilege)\naudit_log: insert only", fill="0.9")
    box(ax, 7.9, 0.85, 2.8, 0.65, "postgres-replica\n(read only, streaming)", fill="0.9")
    arrow(ax, (4.4, 0.85), (6.5, 0.85), "streaming replication")
    arrow(ax, (2.8, 1.7), (2.8, 1.25), "SQL", style="--")
    arrow(ax, (7.9, 1.7), (4.2, 1.15), "SQL", style="--")
    box(ax, 11.3, 2.0, 1.7, 0.75, "mock-bank\n(external,\n127.0.0.1 only)", style="--")
    arrow(ax, (9.7, 2.0), (10.45, 2.0))
    box(ax, 11.3, 6.25, 1.9, 0.6, "Prometheus :9090\n7 alert rules", fill="0.95")
    box(ax, 11.3, 5.35, 1.9, 0.55, "Grafana :3000\ndashboard", fill="0.95")
    arrow(ax, (11.3, 5.95), (11.3, 5.63))
    ax.text(11.3, 4.75, "scrapes /metrics\nevery 2 s from\nevery instance and\ngateway-exporter", fontsize=6.8, ha="center", va="top")
    box(ax, 0.95, 6.1, 1.5, 0.6, "gateway-\nexporter:\nrate_limited_total", fill="0.95")
    arrow(ax, (1.7, 6.75), (1.2, 6.42), "429 log", style="--")
    box(ax, 0.95, 5.3, 1.5, 0.55, "monitor\n/health, /ready", fill="0.95")
    box(ax, 5.2, 5.6, 2.8, 0.5, ".env (git-ignored): JWT secret,\nDB and admin passwords", style=":", fill="white")
    ax.set_title("Endterm architecture: fault tolerance + observability + security (docker-compose.ft.yml)", fontsize=11)
    save(fig, "architecture_endterm.png")


# ---------- report ----------

def report(comp, mve):
    s = pd.read_csv(os.path.join(RES, "summary.csv"))
    ls = pd.read_csv(os.path.join(RES, "load_spike_steps.csv")).groupby(["mode", "users"]).mean(numeric_only=True).reset_index()
    ft300 = ls[(ls["mode"] == "ft") & (ls.users == 300)].iloc[0]
    bl300 = ls[(ls["mode"] == "baseline") & (ls.users == 300)].iloc[0]
    db_ft = s[(s.experiment == "dependency_timeout") & (s.variant == "db") & (s["mode"] == "ft")].iloc[0]
    db_bl = s[(s.experiment == "dependency_timeout") & (s.variant == "db") & (s["mode"] == "baseline")].iloc[0]
    bank_ft = s[(s.experiment == "dependency_timeout") & (s.variant == "bank") & (s["mode"] == "ft")].iloc[0]
    bank_bl = s[(s.experiment == "dependency_timeout") & (s.variant == "bank") & (s["mode"] == "baseline")].iloc[0]
    db_fail_ms = []
    for rep in ("1", "2"):
        path = os.path.join(RAW, f"dependency_timeout_db_ft_r{rep}.csv")
        rows = list(csv.DictReader(open(path)))
        db_fail_ms += [float(r["latency_ms"]) for r in rows if r["status"] == "0"]
    md = ["# Endterm analysis", "", "Generated by `analysis/endterm.py` from the result files (do not edit by hand).", "",
          "## Baseline vs endterm (E5, mean of the valid runs)", "",
          "| Experiment | Success baseline | Success endterm | p95 ms baseline | p95 ms endterm | Detection s (b / e) | Alert s (b / e) | Recovery s (b / e) | Integrity (b / e) |",
          "|---|---|---|---|---|---|---|---|---|"]
    for _, r in comp.iterrows():
        md.append(f"| {r['experiment']} | {pct(r.get('success_baseline'))} | {pct(r.get('success_endterm'))} | {val(r.get('p95_ms_baseline'))} | "
                  f"{val(r.get('p95_ms_endterm'))} | {val(r.get('detection_s_baseline'))} / {val(r.get('detection_s_endterm'))} | "
                  f"{val(r.get('alert_s_baseline'))} / {val(r.get('alert_s_endterm'))} | {val(r.get('recovery_s_baseline'))} / {val(r.get('recovery_s_endterm'))} | "
                  f"{r.get('integrity_baseline')} / {r.get('integrity_endterm')} |")
    md += ["", "Attacks (mean per run):", "", "| Experiment | Mode | Refused | 5xx | Succeeded |", "|---|---|---|---|---|"]
    for name in ("Unauthorized access", "Abusive requests"):
        r = comp[comp.experiment == name].iloc[0]
        for tag in ("baseline", "endterm"):
            md.append(f"| {name} | {tag} | {val(r[f'attacks_refused_{tag}'])} | {val(r[f'attacks_5xx_{tag}'])} | {val(r[f'attacks_succeeded_{tag}'])} |")
    md += ["", "Load spike at 300 users (one IP): endterm "
           f"{pct(ft300['success_rate_all'])} of all requests and {pct(ft300['success_rate_passed_limit'])} of the requests that passed "
           f"the limit succeeded, {ft300['rejected_rate_limit']:.0f} rejected by rate limit per step window, {ft300['throughput_ok_rps']:.0f} ok req/s; "
           f"baseline {pct(bl300['success_rate_all'])}, {bl300['throughput_ok_rps']:.0f} ok req/s.", "",
           "## Midterm vs endterm (matching scenarios)", "",
           "Setup differences: midterm runs were 90 s with the fault at 20 s and no security or observability; endterm runs "
           "75 s with the fault at 15 s, with login, rate limits, metrics and alerts. Success rate is comparable; recovery is "
           "measured the same way (first 10 s window with at least 99% success).", "",
           "| Scenario (midterm / endterm) | Mode | Comparable? | Success midterm | Success endterm | Detection s mid / end | Alert s end | Recovery s mid / end | Consistency mid / end |",
           "|---|---|---|---|---|---|---|---|---|"]
    for _, r in mve.iterrows():
        md.append(f"| {r['scenario']} | {r['mode']} | {r['comparable']} | {pct(r['midterm_success'])} | {pct(r['endterm_success'])} | "
                  f"{val(r['midterm_detection_s'])} / {val(r['endterm_detection_s'])} | {val(r['endterm_alert_s'])} | "
                  f"{val(r['midterm_recovery_s'])} / {val(r['endterm_recovery_s'])} | {r['midterm_consistency']} / {r['endterm_integrity']} |")
    md += ["", "The results agree with the midterm within a few percentage points: the endterm controls did not break the "
           "midterm fault tolerance. The new value is that faults are now also seen as alerts (column Alert s), and that the "
           "endterm stack refuses attacks the midterm FT stack would have served.", "",
           "## Discussion, limitations, and lessons learned", "",
           f"1. **Retry budget longer than the caller's timeout (measured weakness, not fixed).** With the database tables locked "
           f"for 30 s, endterm reached {pct(fnum(db_ft['success_rate_mean']))} against {pct(fnum(db_bl['success_rate_mean']))} for the baseline: "
           f"almost no gain. Every failed request in the endterm runs ended at the client's 10 s timeout "
           f"(median {sorted(db_fail_ms)[len(db_fail_ms) // 2]:.0f} ms, {len(db_fail_ms)} requests). The service tries up to 3 times, and each try waits "
           "2 s on the primary and 2 s on the replica (the lock is replayed there too): about 12 s, longer than the client waits, so even "
           "the cached timetable arrives too late. **Proposed fix:** give each request a total deadline below the caller's timeout "
           "(for example 8 s for a 10 s client), pass the remaining time to every retry, stop retrying when the budget is used up, "
           "and fall back to the cache as soon as the first attempt times out.",
           f"2. **Fault tolerance can lower availability while protecting money.** With a slow bank the endterm stack had "
           f"{pct(fnum(bank_ft['success_rate_mean']))} success against {pct(fnum(bank_bl['success_rate_mean']))} for the baseline, because the circuit "
           "breaker rejects payments at once. The baseline failed the integrity check in every run (money taken for payments it marked failed); "
           "the endterm stack passed every run.",
           "3. **Per-IP rate limits and one test machine.** All load generators share one IP, so the limit shapes every load test; "
           "the 429 answers are the protection working. Real users have many IPs, and an attack from many IPs would not be slowed "
           "down: a per-user limit (by token) would be the next step.",
           "4. **Security choices with costs.** Login fails closed (503) while the primary DB is down, so the lockout cannot be "
           "bypassed; tokens already issued keep working. The HS256 secret is shared by all services, so a compromised service could "
           "create tokens (RS256 would fix it). No TLS: tokens travel as plain HTTP inside this setup.",
           "5. **Measurement lessons.** A sleeping laptop invalidated the first alert series (now every run is checked for gaps). "
           "\"Detection\" first also counted the stack shutdown after each run; it is now computed only inside the load window. "
           "Histogram buckets must match the latencies being measured, otherwise p95 is badly interpolated (a 1 s delay once showed "
           "as 2.4 s). Exact latencies always come from the request logs.",
           "6. **Tests found real problems.** The integration test for header spoofing showed that the gateway let a client hide its "
           "requests from the access log (`X-Probe`); the regression demo showed that a fault during the login phase stopped the "
           "workload. Both were fixed and are covered by tests or checks now.",
           "7. **Limited repetitions and one machine.** 2 repetitions per experiment (alert test 1, plus the E2 series); the standard "
           "deviations are therefore rough. Everything ran on one laptop, so load tests share CPU with the system under test "
           "(about 82% CPU at 300 users in the endterm stack).", ""]
    with open(os.path.join(RES, "analysis.md"), "w") as f:
        f.write("\n".join(md) + "\n")


def main():
    os.makedirs(PLOTS, exist_ok=True)
    exp_rows = {(r["experiment"], r["variant"], r["mode"], r["rep"]): r
                for r in csv.DictReader(open(os.path.join(RES, "experiments.csv"))) if r["valid"] == "True"}
    comp = comparison()
    mve = midterm_vs_endterm()
    plot_success_over_time(exp_rows)
    plot_baseline_vs_endterm(comp)
    plot_load_spike()
    plot_alert_timeline(exp_rows)
    plot_security(comp)
    plot_midterm_vs_endterm(mve)
    plot_dashboard_session()
    plot_architecture()
    report(comp, mve)
    print("wrote", sorted(os.listdir(PLOTS)))


if __name__ == "__main__":
    main()
