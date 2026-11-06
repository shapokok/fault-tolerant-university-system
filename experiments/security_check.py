"""Stage E3 proof against the running endterm stack (docker-compose.ft.yml).

Checks: login and tokens, authorization, input validation, lockout, rate limits, audit log,
DB privileges, secrets in logs and images, and that unauthorized attempts change no data.
Usage: python3 experiments/security_check.py     (about 2 min, waits 61 s for the lockout to end)
Writes results/endterm/e3_security_check.txt. Exit code 1 if any check fails.
"""
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

import auth
from inject import ROOT

G = "http://localhost:8080"
COMPOSE = ["docker", "compose", "-f", "docker-compose.ft.yml"]
results = []  # (section, check, expected, got, verdict)


def call(method, path, body=None, token=None, headers=None, raw=None):
    h = {"content-type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = "Bearer " + token
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(G + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def check(section, name, expected, got):
    ok = got in expected if isinstance(expected, (set, tuple)) else got == expected
    results.append((section, name, expected, got, "PASS" if ok else "FAIL"))
    return ok


def psql(sql, user="uni", password=None):
    """Owner by default (local socket). user=uni_app connects over TCP with its own password."""
    cmd = COMPOSE + ["exec", "-T"] + (["-e", f"PGPASSWORD={password}"] if password else []) + ["postgres-primary", "psql"]
    cmd += ["-h", "localhost"] if password else []
    cmd += ["-U", user, "-d", "uni", "-t", "-A", "-F", ",", "-c", sql]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def make_token(claims, secret, alg="HS256"):
    """Builds a JWT by hand, to send forged, expired and alg=none tokens."""
    def b64(data):
        return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).rstrip(b"=")
    msg = b64({"alg": alg, "typ": "JWT"}) + b"." + b64(claims)
    sig = b"" if alg == "none" else base64.urlsafe_b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).rstrip(b"=")
    return (msg + b"." + sig).decode()


def checksum():
    return psql("SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM payments t") + " / " + \
        psql("SELECT md5(string_agg(t::text, '|' ORDER BY student_id, course_id)) FROM grades t")


def prometheus(expr):
    url = "http://localhost:9090/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    return {tuple(r["metric"].values()): float(r["value"][1]) for r in json.load(urllib.request.urlopen(url))["data"]["result"]}


def main():
    secret = auth.env("JWT_SECRET")
    now = int(time.time())
    t_start = psql("SELECT now()")

    # ---------- A. allowed actions first (they change data on purpose) ----------
    s = "A login"
    code, body = call("POST", "/auth/login", {"username": "student1", "password": auth.env("STUDENT_PASSWORD")})
    check(s, "student1, right password", 200, code)
    t1 = json.loads(body)["access_token"]
    t2 = auth.student_token(2)
    admin = auth.admin_token()
    check(s, "student1, wrong password", 401, call("POST", "/auth/login", {"username": "student1", "password": "wrong"})[0])
    c_unknown, b_unknown = call("POST", "/auth/login", {"username": "nobody", "password": "wrong"})
    c_wrong, b_wrong = call("POST", "/auth/login", {"username": "student1", "password": "wrong"})
    check(s, "unknown user gets the same answer as a wrong password", True, (c_unknown, b_unknown) == (c_wrong, b_wrong))

    s = "B allowed"
    check(s, "student1 reads own record", 200, call("GET", "/students/1", token=t1)[0])
    check(s, "student1 reads own transcript (token passed on to student-service)", 200, call("GET", "/transcripts/1", token=t1)[0])
    code, body = call("POST", "/payments", {"student_id": 1, "amount": 100}, t1, {"Idempotency-Key": str(uuid.uuid4())})
    check(s, "student1 pays for self", 201, code)
    own_payment = json.loads(body)["payment_id"]
    key2 = "check-" + uuid.uuid4().hex
    check(s, "student2 pays for self", 201, call("POST", "/payments", {"student_id": 2, "amount": 50}, t2, {"Idempotency-Key": key2})[0])
    check(s, "admin reads student 2", 200, call("GET", "/students/2", token=admin)[0])
    code, body = call("POST", "/transcripts/batch", token=admin)
    check(s, "admin starts batch transcripts", 202, code)
    time.sleep(12)  # 50 students x 0.2 s
    job = json.loads(call("GET", f"/transcripts/batch/{json.loads(body)['job_id']}", token=admin)[1])
    check(s, "batch job done with 50 transcripts (service token to student-service)", "done/50", f"{job['status']}/{job['transcripts']}")

    # ---------- B. everything below must change nothing ----------
    before = checksum()

    s = "C tokens"
    check(s, "no token", 401, call("GET", "/students/1")[0])
    check(s, "token signed with another secret", 401, call("GET", "/students/1", token=make_token({"sub": "student1", "role": "student", "sid": 1, "exp": now + 600}, "guess"))[0])
    check(s, "forged admin token (another secret)", 401, call("GET", "/students/2", token=make_token({"sub": "admin", "role": "admin", "exp": now + 600}, "guess"))[0])
    check(s, "expired token (right secret)", 401, call("GET", "/students/1", token=make_token({"sub": "student1", "role": "student", "sid": 1, "exp": now - 60}, secret))[0])
    check(s, "alg=none token", 401, call("GET", "/students/1", token=make_token({"sub": "admin", "role": "admin", "exp": now + 600}, "", alg="none"))[0])
    check(s, "garbage token", 401, call("GET", "/students/1", token="not.a.token")[0])

    s = "D authorization"
    check(s, "student1 reads student 2", 403, call("GET", "/students/2", token=t1)[0])
    check(s, "student1 reads timetable of 2", 403, call("GET", "/timetable/2", token=t1)[0])
    check(s, "student1 reads transcript of 2", 403, call("GET", "/transcripts/2", token=t1)[0])
    check(s, "student1 pays for student 2", 403, call("POST", "/payments", {"student_id": 2, "amount": 10}, t1, {"Idempotency-Key": str(uuid.uuid4())})[0])
    check(s, "student1 registers student 2", 403, call("POST", "/registrations", {"student_id": 2, "course_id": 3}, t1)[0])
    check(s, "student1 reuses student 2's payment key", 409, call("POST", "/payments", {"student_id": 1, "amount": 50}, t1, {"Idempotency-Key": key2})[0])
    check(s, "student1 starts batch transcripts (admin only)", 403, call("POST", "/transcripts/batch", token=t1)[0])
    check(s, "student1 reads batch status (admin only)", 403, call("GET", "/transcripts/batch/1", token=t1)[0])
    check(s, "admin pays for student 2 (writes: owner only)", 403, call("POST", "/payments", {"student_id": 2, "amount": 10}, admin, {"Idempotency-Key": str(uuid.uuid4())})[0])
    check(s, "/admin/reconcile through the gateway (not routed)", 404, call("POST", "/admin/reconcile", token=admin)[0])
    check(s, "mock-bank /admin/fault through the gateway (not routed)", 404, call("POST", "/admin/fault", {"mode": "error"}, admin)[0])
    check(s, "/metrics through the gateway (not routed)", 404, call("GET", "/metrics")[0])
    port = subprocess.run(["docker", "port", "uni-ft-mock-bank-1"], capture_output=True, text=True).stdout.strip()
    check(s, "mock-bank port bound to localhost only", True, port.startswith("8000/tcp -> 127.0.0.1:"))

    s = "E validation (422, never 5xx)"
    key = lambda: {"Idempotency-Key": str(uuid.uuid4())}
    bad = [
        ("amount 0", "POST", "/payments", {"student_id": 1, "amount": 0}, key()),
        ("amount 1 000 001", "POST", "/payments", {"student_id": 1, "amount": 1000001}, key()),
        ("amount negative", "POST", "/payments", {"student_id": 1, "amount": -5}, key()),
        ("amount 3 decimals", "POST", "/payments", {"student_id": 1, "amount": 10.123}, key()),
        ("amount text", "POST", "/payments", {"student_id": 1, "amount": "abc"}, key()),
        ("student_id as text", "POST", "/payments", {"student_id": "1", "amount": 10}, key()),
        ("student_id out of range", "POST", "/payments", {"student_id": 9999, "amount": 10}, key()),
        ("student_id 2^63", "POST", "/payments", {"student_id": 2 ** 63, "amount": 10}, key()),
        ("extra field", "POST", "/payments", {"student_id": 1, "amount": 10, "status": "completed"}, key()),
        ("missing amount", "POST", "/payments", {"student_id": 1}, key()),
        ("key with SQL", "POST", "/payments", {"student_id": 1, "amount": 10}, {"Idempotency-Key": "1' OR '1'='1"}),
        ("key 200 characters", "POST", "/payments", {"student_id": 1, "amount": 10}, {"Idempotency-Key": "k" * 200}),
        ("course_id out of range", "POST", "/registrations", {"student_id": 1, "course_id": 99}, None),
        ("path id text", "GET", "/students/abc", None, None),
        ("path id 0", "GET", "/students/0", None, None),
        ("payment id above int32", "GET", "/payments/99999999999", None, None),
        ("login username with SQL", "POST", "/auth/login", {"username": "admin' --", "password": "x"}, None),
        ("login password 5000 characters", "POST", "/auth/login", {"username": "student1", "password": "x" * 5000}, None),
    ]
    for name, method, path, body, headers in bad:
        tok = None if path == "/auth/login" else t1
        check(s, name, 422, call(method, path, body, tok, headers)[0])
    check(s, "malformed JSON", 422, call("POST", "/payments", token=t1, headers=key(), raw=b'{"student_id": 1,')[0])
    check(s, "body of 20 KB (nginx limit 16 KB)", 413, call("POST", "/payments", token=t1, headers=key(), raw=b"x" * 20000)[0])

    after = checksum()
    check("F integrity", "payments and grades unchanged by all attempts above", before, after)

    # ---------- G. lockout ----------
    s = "G lockout"
    for i in range(5):
        call("POST", "/auth/login", {"username": "student3", "password": "wrong"})
    code, body = call("POST", "/auth/login", {"username": "student3", "password": auth.env("STUDENT_PASSWORD")})
    check(s, "right password after 5 failures is refused", 401, code)
    check(s, "message says locked", True, b"too many failed logins" in body)
    print("waiting 61 s for the lock to end ...", flush=True)
    time.sleep(61)
    check(s, "right password after 61 s", 200, call("POST", "/auth/login", {"username": "student3", "password": auth.env("STUDENT_PASSWORD")})[0])

    # ---------- H. rate limits (nginx, per client IP) ----------
    s = "H rate limits"
    time.sleep(2)  # let the login zone refill
    counter_before = sum(prometheus('rate_limited_total').values())
    with ThreadPoolExecutor(20) as pool:
        login_codes = list(pool.map(lambda _: call("POST", "/auth/login", {"username": "student4", "password": auth.env("STUDENT_PASSWORD")})[0], range(40)))
        pay_codes = list(pool.map(lambda _: call("GET", f"/payments/{own_payment}", token=t1)[0], range(120)))
    check(s, f"40 logins at once: 200 {login_codes.count(200)}, 429 {login_codes.count(429)}", True, login_codes.count(429) > 0 and set(login_codes) <= {200, 429})
    check(s, f"120 payment reads at once: 200 {pay_codes.count(200)}, 429 {pay_codes.count(429)}", True, pay_codes.count(429) > 0 and set(pay_codes) <= {200, 429})
    time.sleep(5)  # two scrapes
    counted = sum(prometheus('rate_limited_total').values()) - counter_before
    seen = login_codes.count(429) + pay_codes.count(429)
    check(s, f"rate_limited_total went up by the 429s the client saw", seen, round(counted))

    # ---------- I. audit log ----------
    s = "I audit log"
    rows = psql(f"SELECT action, result, count(*) FROM audit_log WHERE ts >= '{t_start}' GROUP BY 1, 2 ORDER BY 1, 2")
    print("audit rows by action and result:\n  " + rows.replace("\n", "\n  "))
    for action, result in [("login", "success"), ("login", "denied: bad_password"), ("login", "denied: unknown_user"),
                           ("login", "denied: locked"), ("lockout", "locked for 60 s"),
                           ("access", "denied: missing_token"), ("access", "denied: invalid_token"),
                           ("access", "denied: expired_token"), ("access", "denied: forbidden"),
                           ("payment_create", "completed"), ("batch_transcripts", "started")]:
        check(s, f"row for {action} / {result}", True, f"{action},{result}," in rows)
    sample = psql(f"SELECT request_id IS NOT NULL AND source_ip IS NOT NULL FROM audit_log WHERE ts >= '{t_start}' AND result = 'denied: forbidden' LIMIT 1")
    check(s, "rows carry request_id and source_ip", "t", sample)

    # ---------- J. least privilege in the DB (as the app role) ----------
    s = "J DB privileges of uni_app"
    pw = auth.env("APP_DB_PASSWORD")
    for name, sql in [("DROP TABLE", "DROP TABLE payments"), ("TRUNCATE", "TRUNCATE payments"),
                      ("ALTER TABLE", "ALTER TABLE payments ADD COLUMN x int"),
                      ("DELETE payments", "DELETE FROM payments WHERE id = -1"),
                      ("DELETE audit_log", "DELETE FROM audit_log WHERE id = -1"),
                      ("UPDATE audit_log", "UPDATE audit_log SET result = 'x' WHERE id = -1"),
                      ("read audit_log", "SELECT count(*) FROM audit_log")]:
        out = psql(sql, user="uni_app", password=pw)
        check(s, f"{name} refused", True, "permission denied" in out or "must be owner" in out)
    check(s, "normal read works", "50", psql("SELECT count(*) FROM students", user="uni_app", password=pw))

    # ---------- K. secrets ----------
    s = "K secrets"
    names = ["JWT_SECRET", "POSTGRES_PASSWORD", "APP_DB_PASSWORD", "REPLICATOR_PASSWORD", "STUDENT_PASSWORD", "ADMIN_PASSWORD", "GRAFANA_ADMIN_PASSWORD"]
    values = [auth.env(n) for n in names]
    logs = subprocess.run(COMPOSE + ["logs", "--no-color"], cwd=ROOT, capture_output=True, text=True).stdout
    logs += open(os.path.join(ROOT, "results", "endterm", "raw", "e3_dev_monitor.csv")).read() if os.path.exists(os.path.join(ROOT, "results", "endterm", "raw", "e3_dev_monitor.csv")) else ""
    check(s, f"no .env value in {len(logs.splitlines())} log lines", 0, sum(v in logs for v in values))
    check(s, "no issued token in the logs", 0, sum(t in logs for t in (t1, t2, admin)))
    check(s, ".env is ignored by git", ".env", subprocess.run(["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True).stdout.strip())
    check(s, ".env is not tracked", "", subprocess.run(["git", "ls-files", ".env"], cwd=ROOT, capture_output=True, text=True).stdout.strip())
    image = "uni-ft-payment-service-1"
    hits = subprocess.run(["docker", "run", "--rm", "-i", "--entrypoint", "python", image, "-c",
                           "import os,sys; v=sys.stdin.read().split(); print(sum(any(x in open(os.path.join(d,f),errors='ignore').read() for x in v) for d,_,fs in os.walk('/app') for f in fs))"],
                          input="\n".join(values), capture_output=True, text=True).stdout.strip()
    check(s, "no .env value in the service image (/app)", "0", hits)
    env_in_image = subprocess.run(["docker", "image", "inspect", image, "--format", "{{json .Config.Env}}"], capture_output=True, text=True).stdout
    check(s, "no .env value in the image settings", 0, sum(v in env_in_image for v in values))

    # ---------- L. metrics ----------
    s = "L metrics"
    reasons = {k[0]: v for k, v in prometheus('sum by (reason) (auth_failures_total)').items()}
    for r in ("missing_token", "invalid_token", "expired_token", "forbidden", "bad_password", "unknown_user", "locked"):
        check(s, f"auth_failures_total{{reason={r}}} > 0", True, reasons.get(r, 0) > 0)

    # ---------- report ----------
    lines = [f"Stage E3 security check, {time.strftime('%Y-%m-%d %H:%M:%S')}, endterm stack", ""]
    section = None
    for sec, name, expected, got, verdict in results:
        if sec != section:
            lines.append(f"[{sec}]")
            section = sec
        exp = "" if expected is True else f" expected {expected}, got {got}"
        lines.append(f"  {verdict}  {name}{exp}")
    failed = sum(r[4] == "FAIL" for r in results)
    lines += ["", f"{len(results) - failed} of {len(results)} checks passed"]
    text = "\n".join(lines)
    print(text)
    with open(os.path.join(ROOT, "results", "endterm", "e3_security_check.txt"), "w") as f:
        f.write(text + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
