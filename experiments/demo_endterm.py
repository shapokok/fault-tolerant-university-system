"""Endterm demo, 3 scenarios on one fresh endterm stack (docker-compose.ft.yml):

  1. unauthorized_access  no token 401, another student's data 403, audit rows written, data unchanged
  2. abusive_requests     malformed input 4xx (never 5xx), floods get 429, normal users unaffected, integrity PASS
  3. instance_failure     docker stop payment-service-1 for 30 s: InstanceDown fires and resolves, users unaffected

Usage: ./demo_endterm.sh [all|unauthorized|abusive|instance]     DEMO_FAST=1 removes the pauses.
Grafana: http://localhost:3000 (no login), Prometheus alerts: http://localhost:9090/alerts
"""
import collections
import json
import os
import sys
import time
import uuid

import auth
import consistency_check
import e5
from inject import sh
from run_all import start_stack, start_workload
from security_check import call

G = "http://localhost:8080"
FAST = bool(os.getenv("DEMO_FAST"))


def step(text):
    print(f"\n\033[1m== {text}\033[0m", flush=True)


def pause():
    if not FAST:
        input("   (press Enter) ")


def firing():
    data = json.load(__import__("urllib.request").request.urlopen("http://localhost:9090/api/v1/alerts", timeout=5))
    return {(a["labels"]["alertname"], a["labels"].get("instance") or a["labels"].get("service", "")): a["state"]
            for a in data["data"]["alerts"]}


def show_audit(prefix):
    rows = consistency_check.psql("ft", "SELECT to_char(ts, 'HH24:MI:SS'), user_id, action, result, source_ip "
                                        f"FROM audit_log WHERE request_id LIKE '{prefix}%' ORDER BY id")
    for line in rows.splitlines():
        print("   audit (UTC):", line.replace(",", " | "))


def workload_summary(path):
    rows = e5.load_rows(path)
    m = e5.request_metrics(rows)
    print(f"   normal users (workload): {m['total']} requests, {m['success_rate'] * 100:.1f}% ok, p95 {m['p95_ms']} ms")


def unauthorized():
    step("Scenario 1: unauthorized access")
    print("   student1 logs in; then: no token, a forged token, and student1 asking for student 2's data and an admin action")
    pause()
    t1 = auth.student_token(1)
    before = e5.checksum("ft")
    prefix = "demo-" + uuid.uuid4().hex[:8]
    tries = [("no token", "GET", "/students/1", None, None),
             ("forged admin token", "GET", "/students/2", None, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiIsInJvbGUiOiJhZG1pbiJ9.x"),
             ("student1 reads student 2", "GET", "/students/2", None, t1),
             ("student1 pays for student 2", "POST", "/payments", {"student_id": 2, "amount": 10}, t1),
             ("student1 starts batch transcripts (admin only)", "POST", "/transcripts/batch", None, t1),
             ("student1 reads own record", "GET", "/students/1", None, t1)]
    for i, (name, method, path, body, token) in enumerate(tries):
        code = call(method, path, body, token, {"X-Request-ID": f"{prefix}-{i}", "Idempotency-Key": uuid.uuid4().hex})[0]
        print(f"   {name:<48} -> {code}")
    step("Audit log (written by the services, the app role can only INSERT there)")
    show_audit(prefix)
    after = e5.checksum("ft")
    print(f"   payments and grades checksum unchanged: {before == after}")
    pause()


def abusive():
    step("Scenario 2: abusive requests while normal users work")
    print("   workload 20 req/s from its own container; this machine sends 200 malformed requests and 2 bursts of 500")
    pause()
    p, t0, out = e5.with_workload("ft", "demo_abusive", 60)
    time.sleep(10)
    t1 = auth.student_token(1)
    results = e5.send_all(e5.fuzz_requests(200, t1, seed=7), 10)
    fuzz = collections.Counter(s for _, s in results)
    logins = [("login", "POST", "/auth/login", {"username": "student7", "password": "guess"}, None, None, None) for _ in range(500)]
    pays = [("pay", "POST", "/payments", {"student_id": 1, "amount": 1}, t1, {"Idempotency-Key": uuid.uuid4().hex}, None) for _ in range(500)]
    login_codes = collections.Counter(s for _, s in e5.send_all(logins, 50))
    pay_codes = collections.Counter(s for _, s in e5.send_all(pays, 50))
    print(f"   200 malformed requests: {dict(sorted(fuzz.items()))}  (5xx: {sum(n for s, n in fuzz.items() if s >= 500)})")
    print(f"   500 logins at once:     {dict(sorted(login_codes.items()))}")
    print(f"   500 payments at once:   {dict(sorted(pay_codes.items()))}")
    print("   watch Grafana: 429 line and RateLimitSpike")
    p.communicate()
    workload_summary(out)
    passed, reason = consistency_check.check("ft")
    print(f"   integrity: {'PASS' if passed else 'FAIL'} ({reason})")
    pause()


def instance_failure():
    step("Scenario 3: instance failure (docker stop payment-service-1 for 30 s)")
    print("   InstanceDown needs the instance gone for 10 s; watch Grafana: Instances up and Alert history")
    pause()
    p, t0, out = e5.with_workload("ft", "demo_instance", 110)
    time.sleep(10)
    t_stop = time.time()
    sh("docker", "stop", "uni-ft-payment-service-1-1")
    print(f"   {time.strftime('%H:%M:%S')} payment-service-1 stopped")
    last, started = {}, False
    while time.time() - t_stop < 100:
        if not started and time.time() - t_stop >= 30:
            sh("docker", "start", "uni-ft-payment-service-1-1")
            print(f"   {time.strftime('%H:%M:%S')} payment-service-1 started again")
            started = True
        now = firing()
        for key in set(now) | set(last):
            if now.get(key) != last.get(key):
                state = now.get(key, "resolved")
                print(f"   +{time.time() - t_stop:5.1f} s  {key[0]} ({key[1]}): {state}")
        last = now
        if started and not any(k[0] == "InstanceDown" for k in now) and time.time() - t_stop > 40:
            break
        time.sleep(1)
    p.communicate()
    workload_summary(out)
    pause()


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(e5.RAW, exist_ok=True)
    e5.RAW = os.path.join(e5.OUT, "raw", "demo")
    os.makedirs(e5.RAW, exist_ok=True)
    step("Start a fresh endterm stack (about 40 s)")
    start_stack("ft", "endterm/raw/demo/demo_monitor.csv")
    print("   Grafana: http://localhost:3000   Prometheus alerts: http://localhost:9090/alerts")
    if which in ("all", "unauthorized"):
        unauthorized()
    if which in ("all", "abusive"):
        abusive()
    if which in ("all", "instance"):
        instance_failure()
    step("Done. Stop with: docker compose -f docker-compose.ft.yml down -v")


if __name__ == "__main__":
    main()
