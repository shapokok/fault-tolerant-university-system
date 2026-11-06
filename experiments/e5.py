"""Stage E5: the endterm experiments, both modes, 3 repetitions each.

Every run: fresh stack (down -v, up, seed data, readiness), alert recorder, the experiment,
measurements, integrity check, stack down. After every run one row is appended to
results/endterm/experiments.csv, so a crash later loses nothing. Rerunning skips the runs that
already have a valid row (resume). Runs are ordered repetition by repetition.

A run is INVALID (kept in the file, rerun next time) when the alert recorder paused for more than
3 s (laptop asleep), the run took much longer than planned, or the workload sent fewer than 95% of
its planned requests.

Usage: python3 experiments/e5.py --plan                    list of runs and estimated time
       python3 experiments/e5.py                           the full series (about 3 hours)
       python3 experiments/e5.py --smoke                   every experiment once, shortened (code check)
       python3 experiments/e5.py --only load_spike --modes ft --reps 1
"""
import argparse
import collections
import csv
import json
import os
import random
import statistics
import subprocess
import threading
import time
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor

import auth
import consistency_check
import measure
from alert_watch import watch_in_background
from inject import PROJECT, ROOT, bank_fault, container, inject, sh
from run_all import compose, sleep_until, start_stack, start_workload
from security_check import call, make_token

RATE = 20  # workload requests per second
MODES = ("ft", "baseline")
# run_s: warm-up 15 s + fault 30 s + 30 s to see the recovery. alert_test keeps its full lengths.
FULL = {"run_s": 75, "inject_at": 15, "fault_s": 30, "step_s": 60, "settle_s": 20,
        "normal_s": 300, "seg_short": 70, "seg_mid": 100, "seg_long": 110}
SMOKE = {"run_s": 45, "inject_at": 10, "fault_s": 10, "step_s": 20, "settle_s": 15,
         "normal_s": 20, "seg_short": 25, "seg_mid": 30, "seg_long": 30}
T = dict(FULL)
OUT = os.path.join(ROOT, "results", "endterm")
RAW = os.path.join(OUT, "raw", "e5")
TABLES = OUT  # experiments.csv, load_spike_steps.csv, alert_test_e5.csv (smoke: its own folder)
FIELDS = ["experiment", "variant", "mode", "rep", "run_id", "started", "inject_ts", "run_s", "planned_s",
          "total", "success", "rejected_rate_limit", "failed", "success_rate", "success_rate_passed_limit",
          "error_rate", "avg_ms", "p95_ms", "detection_s", "alert_detection_s", "recovery_s",
          "attacks_total", "attacks_blocked", "attacks_5xx", "attacks_succeeded",
          "alerts_expected", "alerts_fired", "alerts_ok", "integrity", "integrity_reason",
          "valid", "invalid_reason", "details"]


def rel(path):
    """Path as seen inside the containers (./results is mounted at /results)."""
    return "/results/" + os.path.relpath(path, os.path.join(ROOT, "results"))


def append_csv(path, row, fields):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


# ---------- measurements ----------

def request_metrics(rows):
    """rows: dicts with status (int) and latency_ms (float). 429 is its own category."""
    total = len(rows)
    ok = sum(1 for r in rows if 200 <= r["status"] < 300)
    rejected = sum(1 for r in rows if r["status"] == 429)
    lat = sorted(r["latency_ms"] for r in rows)
    return {"total": total, "success": ok, "rejected_rate_limit": rejected, "failed": total - ok - rejected,
            "success_rate": round(ok / total, 4) if total else None,
            "success_rate_passed_limit": round(ok / (total - rejected), 4) if total - rejected else None,
            "error_rate": round((total - ok - rejected) / total, 4) if total else None,
            "avg_ms": round(statistics.mean(lat), 1) if lat else None,
            "p95_ms": lat[int(0.95 * (len(lat) - 1))] if lat else None}


def load_rows(path):
    with open(path) as f:
        return [{"ts": float(r["ts"]), "status": int(r["status"]), "latency_ms": float(r["latency_ms"])}
                for r in csv.DictReader(f)]


def checksum(mode):
    return consistency_check.psql(mode, "SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM payments t") + "/" + \
        consistency_check.psql(mode, "SELECT md5(string_agg(t::text, '|' ORDER BY student_id, course_id)) FROM grades t")


def send_all(requests, workers):
    """requests: (kind, method, path, body, token, headers, raw). Returns [(kind, status)]."""
    def one(r):
        kind, method, path, body, token, headers, raw = r
        return kind, call(method, path, body, token, headers, raw)[0]
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(one, requests))


def attack_counts(results):
    statuses = collections.Counter(s for _, s in results)
    by_kind = collections.defaultdict(collections.Counter)
    for kind, s in results:
        by_kind[kind][s] += 1
    return {"attacks_total": len(results),
            "attacks_blocked": sum(n for s, n in statuses.items() if 400 <= s < 500),
            "attacks_5xx": sum(n for s, n in statuses.items() if s >= 500 or s == 0),
            "attacks_succeeded": sum(n for s, n in statuses.items() if 200 <= s < 300),
            "by_kind": {k: dict(v) for k, v in by_kind.items()}}


def save_attacks(run_id, results):
    with open(os.path.join(RAW, f"{run_id}_attacks.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "status"])
        w.writerows(results)


# ---------- faults ----------

def in_background(fn, *args):
    t = threading.Thread(target=fn, args=args)
    t.start()
    return t


def lock_db(mode, seconds):
    """A slow database: one session locks the business tables and sleeps (pg_sleep), so every
    query on them waits. The lock is replayed on the replica too, so reads wait there as well."""
    sql = (f"BEGIN; LOCK TABLE students, registrations, courses, grades, payments, ledger "
           f"IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep({seconds}); COMMIT;")
    subprocess.run(["docker", "compose", "-f", f"docker-compose.{mode}.yml", "exec", "-T", "postgres-primary",
                    "psql", "-U", "uni", "-d", "uni", "-c", sql], cwd=ROOT, capture_output=True)


def payment_name(mode):
    return container(mode, "payment-service-1" if mode == "ft" else "payment-service")


def stop_start(mode, seconds):
    sh("docker", "stop", payment_name(mode))
    time.sleep(seconds)
    sh("docker", "start", payment_name(mode))


def bank_for(bank_mode, seconds, delay_s=0):
    bank_fault(bank_mode, delay_s)
    time.sleep(seconds)
    bank_fault("none")


def flood(seconds, rate, make_request):
    """Sends make_request(i) at a steady rate for `seconds`. Returns [(kind, status)]."""
    out, start, i = [], time.time(), 0
    while time.time() - start < seconds:
        kind, method, path, body, token, headers = make_request(i)
        out.append((kind, call(method, path, body, token, headers)[0]))
        i += 1
        time.sleep(max(0, start + i / rate - time.time()))
    return out


def unauthorized_flood(token, seconds):
    def req(i):
        if i % 2:
            return "read_other", "GET", "/students/2", None, token, None
        return "no_token", "GET", "/students/1", None, None, None
    return flood(seconds, 2, req)


def rate_flood(token, pid, seconds):
    return flood(seconds, 40, lambda i: ("payment_read", "GET", f"/payments/{pid}", None, token, None))


# ---------- experiments ----------
# Each returns a dict: workload (CSV path or None), inject_ts, planned_s, planned_requests,
# expected (alerts), integrity (passed, reason), and optional attack counts and details.

def with_workload(mode, run_id, seconds):
    out = os.path.join(RAW, f"{run_id}.csv")
    p, t0 = start_workload(mode, seconds, rel(out))
    return p, t0, out


def workload_run(mode, run_id, fault, expected):
    """Workload for run_s; fault() is called at inject_at and may block (it runs in a thread)."""
    p, t0, out = with_workload(mode, run_id, T["run_s"])
    sleep_until(t0 + T["inject_at"])
    t_inject = time.time()
    th = in_background(fault)
    p.communicate()
    th.join()
    time.sleep(3)
    return {"workload": out, "inject_ts": t_inject, "planned_s": T["run_s"], "planned_requests": RATE * T["run_s"],
            "expected": expected, "integrity": consistency_check.check(mode)}


def instance_failure(mode, variant, run_id):
    return workload_run(mode, run_id, lambda: inject("app_crash", mode),
                        {"InstanceRestarted"} if mode == "ft" else {"InstanceDown"})


def dependency_timeout(mode, variant, run_id):
    if variant == "bank":  # mock-bank answers after 5 s, for 30 s
        fault = lambda: bank_for("delay", T["fault_s"], 5)  # noqa: E731
    else:                  # DB queries wait on locked tables for 30 s
        fault = lambda: lock_db(mode, T["fault_s"])  # noqa: E731
    return workload_run(mode, run_id, fault, {"HighLatencyP95"})


def recovery_consistency(mode, variant, run_id):
    """crash_after_debit for 20 s, then a slow bank and kill -9 of a payment instance mid-request."""
    def fault():
        bank_for("crash_after_debit", T["fault_s"] * 2 / 3)
        bank_fault("delay", 1.5)          # payments are now in flight for 1.5 s
        time.sleep(T["fault_s"] / 6)
        inject("app_crash", mode)         # kill -9 while requests are in flight
        time.sleep(T["fault_s"] / 3)
        bank_fault("none")
    return workload_run(mode, run_id, fault, {"InstanceRestarted"} if mode == "ft" else {"InstanceDown"})


def unauthorized_access(mode, variant, run_id):
    """No background load (so any change of payments or grades can only come from an attack)."""
    secure = auth.secure(mode)
    t1 = auth.student_token(1) if secure else None
    secret = auth.env("JWT_SECRET")
    now = int(time.time())
    forged = make_token({"sub": "admin", "role": "admin", "exp": now + 600}, "guess")
    expired = make_token({"sub": "student1", "role": "student", "sid": 1, "exp": now - 60}, secret)
    kinds = [("no_token", "GET", "/students/2", None, None), ("forged_admin_token", "GET", "/students/2", None, forged),
             ("expired_token", "GET", "/students/1", None, expired), ("read_other_student", "GET", "/students/2", None, t1),
             ("read_other_timetable", "GET", "/timetable/2", None, t1), ("read_other_transcript", "GET", "/transcripts/2", None, t1),
             ("pay_for_other", "POST", "/payments", {"student_id": 2, "amount": 10}, t1),
             ("admin_endpoint", "POST", "/transcripts/batch", None, t1)]
    before = checksum(mode)
    t_inject, results = time.time(), []
    for i in range(10):  # 10 rounds x 8 kinds = 80 attempts, about 8 per second
        for kind, method, path, body, token in kinds:
            headers = {"X-Request-ID": f"e5-{run_id}-{len(results)}", "Idempotency-Key": uuid.uuid4().hex}
            results.append((kind, call(method, path, body, token, headers)[0]))
            time.sleep(0.12)
    time.sleep(T["settle_s"])  # let the alert fire
    after = checksum(mode)
    audit = consistency_check.psql(mode, f"SELECT count(*) FROM audit_log WHERE request_id LIKE 'e5-{run_id}-%'") \
        if secure else "no audit log"
    save_attacks(run_id, results)
    counts = attack_counts(results)
    return {"workload": None, "inject_ts": t_inject, "planned_s": T["settle_s"] + 15, "planned_requests": None,
            "expected": {"AuthFailureSpike"} if secure else set(),
            "integrity": (before == after, "payments and grades unchanged" if before == after else "payments or grades changed"),
            "attacks": counts, "details": {"audit_rows": audit, "by_kind": counts.pop("by_kind")}}


def fuzz_requests(n, token, seed):
    rnd = random.Random(seed)
    weird = [None, True, -1, 0, 2 ** 63, -2 ** 63, 1e308, "1", "", "x" * 3000, "' OR '1'='1", "1; DROP TABLE payments; --",
             {"a": 1}, [1, 2], "\u0000", "é" * 50, 3.14159, -0.01, "NaN", 9999]
    valid = {"/payments": {"student_id": 1, "amount": 100}, "/registrations": {"student_id": 1, "course_id": 2},
             "/auth/login": {"username": "student1", "password": "wrong-password"}}
    out = []
    for i in range(n):
        path = rnd.choice(list(valid))
        body, raw, how = dict(valid[path]), None, rnd.choice(["value", "value", "drop", "extra", "not_object", "bad_json", "huge"])
        if how == "value":
            body[rnd.choice(list(body))] = rnd.choice(weird)
        elif how == "drop":
            body.pop(rnd.choice(list(body)))
        elif how == "extra":
            body[rnd.choice(["status", "role", "admin", "id"])] = rnd.choice(weird)
        elif how == "not_object":
            body = rnd.choice([[1, 2, 3], "text", 42, None])
        elif how == "bad_json":
            raw = rnd.choice([b'{"student_id": 1,', b"{'a': 1}", b"\xff\xfe", b"[[[[[[[[", b'{"amount": }'])
        else:
            raw = json.dumps({**body, "pad": "x" * 20000}).encode()
        headers = {"Idempotency-Key": uuid.uuid4().hex} if path == "/payments" else {}
        out.append((f"fuzz_{how}", "POST", path, body if raw is None else None, token if path != "/auth/login" else None, headers, raw))
    return out


def abusive_requests(mode, variant, run_id):
    """Normal workload (other IP) while this machine sends 500 malformed requests and two 1000-request bursts."""
    secure = auth.secure(mode)
    t1 = auth.student_token(1) if secure else None
    p, t0, out = with_workload(mode, run_id, T["run_s"])
    sleep_until(t0 + T["inject_at"])
    t_inject = time.time()
    results = send_all(fuzz_requests(500, t1, seed=zlib.crc32(run_id.encode())), 10)  # same cases every time for this run id
    rnd = random.Random(1)
    logins = [("login_burst", "POST", "/auth/login", {"username": f"student{rnd.randint(1, 50)}", "password": "guess"}, None, None, None)
              for _ in range(1000)]
    pays = [("payment_burst", "POST", "/payments", {"student_id": 1, "amount": 1}, t1, {"Idempotency-Key": uuid.uuid4().hex}, None)
            for _ in range(1000)]
    results += send_all(logins, 50) + send_all(pays, 50)
    p.communicate()
    time.sleep(3)
    save_attacks(run_id, results)
    counts = attack_counts(results)
    monitor = os.path.join(RAW, f"{run_id}_monitor.csv")
    t_load_end = t0 + T["run_s"]  # not the shutdown afterwards, when every instance stops
    down = sum(1 for r in csv.DictReader(open(monitor)) if r["state"] == "down" and t_inject <= float(r["ts"]) < t_load_end) \
        if os.path.exists(monitor) else None
    return {"workload": out, "inject_ts": t_inject, "planned_s": T["run_s"], "planned_requests": RATE * T["run_s"],
            "expected": {"RateLimitSpike"} if secure else set(), "integrity": consistency_check.check(mode),
            "attacks": counts, "details": {"by_kind": counts.pop("by_kind"), "monitor_down_events": down}}


def docker_stats(project, stop, rows):
    while not stop.is_set():
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}};{{.CPUPerc}};{{.MemUsage}}"],
                             capture_output=True, text=True).stdout
        ts = time.time()
        for line in out.splitlines():
            name, cpu, mem = line.split(";")
            if name.startswith(project):
                used = mem.split("/")[0].strip()
                mib = float(used[:-3]) * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}.get(used[-3:], 1) if used[-3:] in ("KiB", "MiB", "GiB") else 0
                rows.append((round(ts, 1), name, float(cpu.rstrip("%") or 0), round(mib, 1)))
        stop.wait(3)


def load_spike(mode, variant, run_id, hold=False):
    """Locust 50 -> 150 -> 300 users, 60 s each, from this machine's Docker network (one IP)."""
    log = os.path.join(RAW, f"{run_id}_locust.csv")
    env = ["-e", f"LOCUST_LOG=/mnt/out/{run_id}_locust.csv", "-e", f"LOCUST_STEP_S={T['step_s']}"]
    if auth.secure(mode):
        env += ["-e", "SECURITY_MODE=on", "-e", f"STUDENT_PASSWORD={auth.env('STUDENT_PASSWORD')}"]
    if hold:
        env += ["-e", "LOCUST_HOLD=1"]
    cmd = ["docker", "run", "--rm", "--name", f"e5-locust-{mode}", "--network", f"{PROJECT[mode]}_default", *env,
           "-v", f"{ROOT}/experiments:/mnt/exp:ro", "-v", f"{RAW}:/mnt/out",
           "locustio/locust", "-f", "/mnt/exp/locust_steps.py", "--headless", "--host", "http://gateway", "--only-summary"]
    if hold:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop, stats = threading.Event(), []
    sampler = in_background(docker_stats, PROJECT[mode], stop, stats)
    t_inject = time.time()
    subprocess.run(cmd, capture_output=True)
    stop.set()
    sampler.join()
    with open(os.path.join(RAW, f"{run_id}_docker_stats.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "container", "cpu_percent", "mem_mib"])
        w.writerows(stats)

    rows = load_rows(log)
    t0 = min(r["ts"] for r in rows)
    steps = []
    for i, users in enumerate((50, 150, 300)):
        a, b = t0 + i * T["step_s"] + T["step_s"] / 3, t0 + (i + 1) * T["step_s"]  # skip the first third (ramp up)
        sel = [r for r in rows if a <= r["ts"] < b]
        m = request_metrics(sel)
        ok_lat = sorted(r["latency_ms"] for r in sel if 200 <= r["status"] < 300)
        cpu = collections.defaultdict(float)
        mem = collections.defaultdict(float)
        for ts, name, c, mib in stats:
            if a <= ts < b:
                cpu[ts] += c
                mem[ts] += mib
        steps.append({"run_id": run_id, "mode": mode, "users": users, "window_s": round(b - a),
                      "requests": m["total"], "ok": m["success"], "rejected_rate_limit": m["rejected_rate_limit"],
                      "errors_5xx": sum(1 for r in sel if r["status"] >= 500),
                      "no_answer": sum(1 for r in sel if r["status"] == 0),
                      "other_4xx": sum(1 for r in sel if 400 <= r["status"] < 500 and r["status"] != 429),
                      "success_rate_all": m["success_rate"], "success_rate_passed_limit": m["success_rate_passed_limit"],
                      "throughput_ok_rps": round(m["success"] / (b - a), 1),
                      "p50_ok_ms": ok_lat[len(ok_lat) // 2] if ok_lat else None,
                      "p95_ok_ms": ok_lat[int(0.95 * (len(ok_lat) - 1))] if ok_lat else None,
                      "cpu_total_percent": round(statistics.mean(cpu.values()), 1) if cpu else None,
                      "mem_total_mib": round(max(mem.values())) if mem else None})
    for s in steps:
        append_csv(os.path.join(TABLES, "load_spike_steps.csv"), s, list(s))
    return {"workload": None, "requests": rows, "inject_ts": t_inject, "planned_s": 3 * T["step_s"] + 20,
            "planned_requests": None, "expected": set(), "integrity": consistency_check.check(mode),
            "details": {"steps": steps}}


def alert_test(mode, variant, run_id):
    """One stack, workload all the time: 5 min normal load (no alert may fire), then each rule on purpose."""
    secure = auth.secure(mode)
    t1 = auth.student_token(1) if secure else None
    pid = json.loads(call("POST", "/payments", {"student_id": 1, "amount": 5}, t1, {"Idempotency-Key": uuid.uuid4().hex})[1])["payment_id"]
    F = T["fault_s"]
    segments = [("normal load", T["normal_s"], None, set(), set())]
    if mode == "ft":
        segments += [
            ("kill -9 payment-service-1", T["seg_short"], lambda: inject("app_crash", mode), {"InstanceRestarted"}, {"InstanceDown"}),
            ("docker stop 30 s", T["seg_mid"], lambda: stop_start(mode, F), {"InstanceDown"}, set()),
            ("bank error 30 s", T["seg_long"], lambda: bank_for("error", F), {"HighErrorRate", "CircuitBreakerOpen"}, set()),
            ("bank delay 1 s for 45 s", T["seg_long"], lambda: bank_for("delay", F * 1.5, 1), {"HighLatencyP95"}, set()),
            ("401/403 flood 30 s", T["seg_mid"], lambda: unauthorized_flood(t1, F), {"AuthFailureSpike"}, set()),
            ("rate limit flood 30 s", T["seg_mid"], lambda: rate_flood(t1, pid, F), {"RateLimitSpike"}, set())]
    else:  # no auth and no rate limit in the baseline; kill -9 last (no restart policy)
        segments += [
            ("bank error 30 s", T["seg_long"], lambda: bank_for("error", F), {"HighErrorRate"}, {"CircuitBreakerOpen"}),
            ("bank delay 1 s for 45 s", T["seg_long"], lambda: bank_for("delay", F * 1.5, 1), {"HighLatencyP95"}, set()),
            ("docker stop 30 s", T["seg_mid"], lambda: stop_start(mode, F), {"InstanceDown"}, set()),
            ("kill -9 payment-service", T["seg_short"], lambda: inject("app_crash", mode), {"InstanceDown"}, {"InstanceRestarted"})]
    total_s = sum(s[1] for s in segments)
    p, t0, out = with_workload(mode, run_id, total_s + 5)
    starts = []
    for name, seconds, fault, _, _ in segments:
        start = time.time()
        starts.append(start)
        th = in_background(fault) if fault else None
        sleep_until(start + seconds)
        if th:
            th.join()
    p.communicate()
    return {"workload": out, "inject_ts": t0, "planned_s": total_s + 5, "planned_requests": RATE * (total_s + 5),
            "expected": set().union(*(s[3] for s in segments)), "integrity": consistency_check.check(mode),
            "segments": [(name, start, seconds, exp, must_not) for (name, seconds, _, exp, must_not), start in zip(segments, starts)]}


EXPERIMENTS = [("instance_failure", "kill -9", instance_failure),
               ("dependency_timeout", "bank", dependency_timeout),
               ("dependency_timeout", "db", dependency_timeout),
               ("load_spike", "50-150-300 users", load_spike),
               ("alert_test", "all rules + 5 min normal", alert_test),
               ("unauthorized_access", "8 attack kinds", unauthorized_access),
               ("abusive_requests", "fuzz + bursts", abusive_requests),
               ("recovery_consistency", "crash after debit + kill", recovery_consistency)]
ESTIMATE_MIN = {("instance_failure", "ft"): 2.1, ("instance_failure", "baseline"): 1.75,
                ("dependency_timeout", "ft"): 2.1, ("dependency_timeout", "baseline"): 1.75,
                ("load_spike", "ft"): 4.1, ("load_spike", "baseline"): 3.6,
                ("alert_test", "ft"): 16, ("alert_test", "baseline"): 12.2,
                ("unauthorized_access", "ft"): 1.3, ("unauthorized_access", "baseline"): 1,
                ("abusive_requests", "ft"): 2.1, ("abusive_requests", "baseline"): 1.75,
                ("recovery_consistency", "ft"): 2.1, ("recovery_consistency", "baseline"): 1.75}
REPS = {"alert_test": 1}  # agreed with the user: 1 repetition (E2 has a second, independent series)


# ---------- one run ----------

def run(experiment, variant, fn, mode, rep, out_csv):
    run_id = f"{experiment}_{variant.split()[0].replace('-', '')}_{mode}_r{rep}"
    print(f"\n=== {run_id}  {time.strftime('%H:%M:%S')}", flush=True)
    start_stack(mode, os.path.relpath(os.path.join(RAW, f"{run_id}_monitor.csv"), os.path.join(ROOT, "results")))
    stop, watcher, alerts, stats = watch_in_background(run_id, RAW)
    t_start = time.time()
    try:
        res = fn(mode, variant, run_id)
    finally:
        t_end = time.time()
        stop.set()
        watcher.join()
        compose(mode, "down", "-v")
    t_inject = res["inject_ts"]

    if res.get("workload"):
        rows = load_rows(res["workload"])
    else:
        rows = res.get("requests", [])
    m = request_metrics(rows) if rows else {}
    firing = [a for a in alerts if a["state"] == "firing" and a["ts"] >= t_inject]
    fired = sorted({a["alertname"] for a in firing})
    expected = res["expected"]
    first_expected = [a["ts"] for a in firing if a["alertname"] in expected]
    # Detection is only meaningful for an infrastructure fault; alert_test has its own delay per segment.
    no_fault = ("alert_test", "load_spike", "unauthorized_access", "abusive_requests")
    detection = None if experiment in no_fault else measure.detection_time(os.path.join(RAW, f"{run_id}_monitor.csv"), t_inject, t_end)
    monitor_csv = os.path.join(RAW, f"{run_id}_monitor.csv")
    if os.path.exists(monitor_csv):  # what the monitor saw after the start, for every experiment
        details = dict(res.get("details", {}))
        details["monitor_states_after_start"] = dict(collections.Counter(
            r["state"] for r in csv.DictReader(open(monitor_csv)) if t_inject <= float(r["ts"]) < t_end and r["state"] != "up"))
        res["details"] = details
    recovery = measure.recovery_time(measure.load_requests(res["workload"]), t_inject) if res.get("workload") and experiment != "alert_test" else None
    details = res.get("details", {})

    if experiment == "alert_test":  # one verdict per segment
        seg_rows, all_ok = [], True
        for name, start, seconds, exp, must_not in res["segments"]:
            got = {a["alertname"] for a in alerts if a["state"] == "firing" and start <= a["ts"] < start + seconds}
            delays = [a["ts"] - start for a in alerts if a["state"] == "firing" and a["alertname"] in exp and start <= a["ts"] < start + seconds]
            ok = exp <= got and not (got & must_not) and (bool(exp) or not got)
            all_ok &= ok
            seg = {"run_id": run_id, "mode": mode, "rep": rep, "segment": name, "expected": " ".join(sorted(exp)),
                   "fired": " ".join(sorted(got)), "first_expected_after_s": round(min(delays), 1) if delays else "",
                   "verdict": "PASS" if ok else "FAIL"}
            seg_rows.append(seg)
            append_csv(os.path.join(TABLES, "alert_test_e5.csv"), seg, list(seg))
        details["segments"] = seg_rows
        alerts_ok = all_ok
        alert_delay = round(statistics.mean(float(s["first_expected_after_s"]) for s in seg_rows if s["first_expected_after_s"] != ""), 1) \
            if any(s["first_expected_after_s"] != "" for s in seg_rows) else None
    else:
        alerts_ok = expected <= set(fired)
        alert_delay = round(min(first_expected) - t_inject, 1) if first_expected else None

    invalid = []
    if stats.get("max_gap_s", 0) > 3:
        invalid.append(f"alert recorder paused {stats['max_gap_s']:.0f} s")
    if t_end - t_start > res["planned_s"] + 90:
        invalid.append(f"run took {t_end - t_start:.0f} s, planned about {res['planned_s']} s")
    if res.get("planned_requests") and m.get("total", 0) < 0.95 * res["planned_requests"]:
        invalid.append(f"workload sent {m.get('total', 0)} of {res['planned_requests']} requests")
    passed, reason = res["integrity"]
    row = {"experiment": experiment, "variant": variant, "mode": mode, "rep": rep, "run_id": run_id,
           "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_start)), "inject_ts": round(t_inject, 3),
           "run_s": round(t_end - t_start), "planned_s": res["planned_s"], **{k: m.get(k) for k in
           ("total", "success", "rejected_rate_limit", "failed", "success_rate", "success_rate_passed_limit", "error_rate", "avg_ms", "p95_ms")},
           "detection_s": detection, "alert_detection_s": alert_delay, "recovery_s": recovery,
           **res.get("attacks", {}), "alerts_expected": " ".join(sorted(expected)), "alerts_fired": " ".join(fired),
           "alerts_ok": alerts_ok, "integrity": "PASS" if passed else "FAIL", "integrity_reason": reason,
           "valid": not invalid, "invalid_reason": "; ".join(invalid), "details": json.dumps(details)}
    append_csv(out_csv, row, FIELDS)
    print({k: row.get(k) for k in ("total", "success_rate", "p95_ms", "detection_s", "alert_detection_s", "recovery_s",
                               "attacks_total", "attacks_blocked", "attacks_5xx", "alerts_fired", "alerts_ok",
                               "integrity", "valid", "invalid_reason")}, flush=True)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--reps", type=int, default=2)  # agreed with the user (option B): 2, alert_test 1
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--plan", action="store_true")
    args = ap.parse_args()
    global RAW, TABLES
    out_csv = os.path.join(OUT, "experiments.csv")
    if args.smoke:
        T.update(SMOKE)
        RAW = TABLES = os.path.join(OUT, "e5_smoke")
        out_csv = os.path.join(RAW, "experiments.csv")
        args.reps = 1
    os.makedirs(RAW, exist_ok=True)
    chosen = [e for e in EXPERIMENTS if not args.only or e[0] in args.only.split(",")]
    plan = [(e, v, fn, mode, rep) for rep in range(1, args.reps + 1) for e, v, fn in chosen
            for mode in args.modes.split(",") if rep <= REPS.get(e, args.reps)]
    done = set()
    if os.path.exists(out_csv):
        done = {(r["experiment"], r["variant"], r["mode"], int(r["rep"])) for r in csv.DictReader(open(out_csv)) if r["valid"] == "True"}
    todo = [p for p in plan if (p[0], p[1], p[3], p[4]) not in done]
    minutes = sum(ESTIMATE_MIN[(p[0], p[3])] for p in todo) * (0.35 if args.smoke else 1)
    print(f"{len(todo)} of {len(plan)} runs to do, about {minutes:.0f} min", flush=True)
    if args.plan:
        for p in todo:
            print(f"  rep {p[4]}  {p[0]:<22}{p[1]:<28}{p[3]}")
        return
    for experiment, variant, fn, mode, rep in todo:
        try:
            run(experiment, variant, fn, mode, rep, out_csv)
        except Exception as e:  # keep going: one broken run must not stop the series
            print(f"run {experiment}/{variant}/{mode}/r{rep} crashed: {e!r}", flush=True)
            for m in MODES:
                compose(m, "down", "-v")
    print("\nseries finished", time.strftime("%H:%M:%S"), flush=True)


if __name__ == "__main__":
    main()
