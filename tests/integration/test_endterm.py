"""Integration tests against the running endterm stack. Each docstring is the acceptance criterion."""
import json
import re
import subprocess
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

import auth
import consistency_check
from inject import ROOT, bank_fault
from security_check import COMPOSE, call, prometheus, psql


def new_key():
    return {"Idempotency-Key": str(uuid.uuid4())}


def bank_charges(ref):
    return json.load(urllib.request.urlopen(f"http://localhost:8090/charges?ref={ref}", timeout=5))


def audit_rows(request_id):
    return psql(f"SELECT user_id, action, result, source_ip FROM audit_log WHERE request_id = '{request_id}'")


def gateway_logs():
    return subprocess.run(COMPOSE + ["logs", "--no-color", "gateway"], cwd=ROOT, capture_output=True, text=True).stdout


# ---------- authentication and authorization ----------

def test_no_token_401():
    """Every business endpoint without a token answers 401."""
    for method, path in [("GET", "/students/1"), ("GET", "/timetable/1"), ("GET", "/transcripts/1"),
                         ("POST", "/payments"), ("POST", "/registrations"), ("GET", "/payments/1")]:
        assert call(method, path, body={})[0] == 401, path


def test_other_students_data_403(student1):
    """student1 reading or changing student 2's data gets 403."""
    assert call("GET", "/students/2", token=student1)[0] == 403
    assert call("GET", "/timetable/2", token=student1)[0] == 403
    assert call("GET", "/transcripts/2", token=student1)[0] == 403
    assert call("POST", "/payments", {"student_id": 2, "amount": 10}, student1, new_key())[0] == 403
    assert call("POST", "/registrations", {"student_id": 2, "course_id": 1}, student1)[0] == 403


def test_student_cannot_use_admin_endpoints(student1):
    """A student calling admin endpoints (batch transcripts) gets 403."""
    assert call("POST", "/transcripts/batch", token=student1)[0] == 403
    assert call("GET", "/transcripts/batch/1", token=student1)[0] == 403


@pytest.mark.parametrize("body", [
    {"student_id": 1, "amount": 0},
    {"student_id": 1, "amount": "abc"},
    {"student_id": "1", "amount": 10},
    {"student_id": 9999, "amount": 10},
    {"student_id": 1, "amount": 10, "status": "completed"},
    {"student_id": 1},
], ids=["amount 0", "amount text", "id as text", "id out of range", "extra field", "missing amount"])
def test_invalid_payment_422(student1, body):
    """An invalid payment body is answered 422, never 5xx."""
    assert call("POST", "/payments", body, student1, new_key())[0] == 422


# ---------- idempotency and audit ----------

def test_idempotent_payment(student1):
    """The same Idempotency-Key twice: 201 then 200 with the same payment, one DB row, one bank charge."""
    key = "itest-" + uuid.uuid4().hex
    first = call("POST", "/payments", {"student_id": 1, "amount": 25}, student1, {"Idempotency-Key": key})
    second = call("POST", "/payments", {"student_id": 1, "amount": 25}, student1, {"Idempotency-Key": key})
    assert first[0] == 201 and second[0] == 200
    assert json.loads(second[1])["replayed"] is True
    assert json.loads(first[1])["payment_id"] == json.loads(second[1])["payment_id"]
    assert psql(f"SELECT count(*) FROM payments WHERE idempotency_key = '{key}'") == "1"
    assert len(bank_charges(key)) == 1


def test_audit_row_for_denied_request(student1):
    """A refused request writes an audit row with its request_id, user, action, result and source IP."""
    rid = "itest-" + uuid.uuid4().hex
    assert call("GET", "/students/2", token=student1, headers={"X-Request-ID": rid})[0] == 403
    user, action, result, ip = audit_rows(rid).split(",")
    assert (user, action, result) == ("student1", "access", "denied: forbidden") and ip


def test_audit_row_for_payment(student1):
    """A payment writes an audit row (payment_create, completed)."""
    rid = "itest-" + uuid.uuid4().hex
    assert call("POST", "/payments", {"student_id": 1, "amount": 5}, student1, {**new_key(), "X-Request-ID": rid})[0] == 201
    assert audit_rows(rid).split(",")[1:3] == ["payment_create", "completed"]


# ---------- header spoofing ----------

def test_x_real_ip_cannot_be_spoofed(student1):
    """A client-sent X-Real-IP / X-Forwarded-For does not reach the audit log; the gateway's view of the client does."""
    rid = "itest-" + uuid.uuid4().hex
    call("GET", "/students/2", token=student1, headers={"X-Request-ID": rid, "X-Real-IP": "6.6.6.6", "X-Forwarded-For": "6.6.6.6"})
    ip = audit_rows(rid).split(",")[3]
    assert ip and ip != "6.6.6.6"


def test_x_probe_cannot_hide_traffic(student1):
    """Requests with X-Probe: 1 from a client are counted as user traffic (not probe) and appear in the gateway log."""
    query = 'sum by (source) (http_requests_total{service="student-service",endpoint="/timetable/{sid}"})'
    before = {k[0]: v for k, v in prometheus(query).items()}
    rids = ["itest-" + uuid.uuid4().hex for _ in range(10)]
    for rid in rids:
        assert call("GET", "/timetable/1", token=student1, headers={"X-Probe": "1", "X-Request-ID": rid})[0] == 200
    time.sleep(5)  # two scrapes
    after = {k[0]: v for k, v in prometheus(query).items()}
    assert after.get("probe", 0) == before.get("probe", 0)
    assert after.get("user", 0) - before.get("user", 0) >= 10
    logs = gateway_logs()
    assert all(rid in logs for rid in rids), "requests with X-Probe are missing from the gateway log"


def test_user_id_in_service_logs(student1):
    """The access log line of an authenticated request carries the user (student1) in every service it passes."""
    rid = "itest-" + uuid.uuid4().hex
    assert call("GET", "/transcripts/1", token=student1, headers={"X-Request-ID": rid})[0] == 200
    time.sleep(1)
    logs = subprocess.run(COMPOSE + ["logs", "--no-color", "--no-log-prefix", "transcript-service-1", "transcript-service-2",
                                     "student-service-1", "student-service-2"], cwd=ROOT, capture_output=True, text=True).stdout
    lines = [json.loads(line) for line in logs.splitlines() if rid in line and '"http_request"' in line]
    assert {entry["service"] for entry in lines} == {"transcript-service", "student-service"}
    assert all(entry["user_id"] == "student1" for entry in lines), [entry["user_id"] for entry in lines]


def test_bad_request_id_is_replaced():
    """A client X-Request-ID that is not 1 to 64 safe characters is replaced by a new ID."""
    req = urllib.request.Request("http://localhost:8080/health", headers={"X-Request-ID": "bad id $(x)"})
    rid = urllib.request.urlopen(req, timeout=5).headers["X-Request-ID"]
    assert rid != "bad id $(x)" and re.fullmatch(r"[0-9a-f]{32}", rid)


# ---------- secrets ----------

def test_no_secrets_in_logs(student1, student2, admin):
    """No value from .env and no issued token appears in any container log."""
    logs = subprocess.run(COMPOSE + ["logs", "--no-color"], cwd=ROOT, capture_output=True, text=True).stdout
    names = ["JWT_SECRET", "POSTGRES_PASSWORD", "APP_DB_PASSWORD", "REPLICATOR_PASSWORD", "STUDENT_PASSWORD",
             "ADMIN_PASSWORD", "GRAFANA_ADMIN_PASSWORD"]
    assert [n for n in names if auth.env(n) in logs] == []
    assert not any(t in logs for t in (student1, student2, admin))


# ---------- recovery: consistency after a fault ----------

def test_consistency_after_bank_crash(student1):
    """After bank crash_after_debit during payments and reconciliation: no duplicate, lost or pending payment."""
    keys = ["itest-" + uuid.uuid4().hex for _ in range(8)]
    bank_fault("crash_after_debit")
    try:
        codes = [call("POST", "/payments", {"student_id": 1, "amount": 30}, student1, {"Idempotency-Key": k})[0] for k in keys]
    finally:
        bank_fault("none")
    assert set(codes) <= {202, 502, 503}, codes  # unknown outcome (pending) or refused, never "completed"
    passed, reason = consistency_check.check("ft")  # reconciles first, as admin
    assert passed, reason
    statuses = psql("SELECT status FROM payments WHERE idempotency_key IN (" + ",".join(f"'{k}'" for k in keys) + ")").split()
    assert "pending" not in statuses and len(statuses) == len(keys)


# ---------- rate limit (last: it uses up this machine's payment burst) ----------

def test_burst_gets_429(student1):
    """120 payment reads at once from one IP: some get 429 from the gateway, none 5xx, and rate_limited_total counts each 429."""
    # Read an existing payment: creating one now could hit the bank breaker that the previous test opened.
    pid = psql("SELECT id FROM payments WHERE student_id = 1 AND status = 'completed' ORDER BY id LIMIT 1")
    if not pid:  # test run on its own
        pid = json.loads(call("POST", "/payments", {"student_id": 1, "amount": 7}, student1, new_key())[1])["payment_id"]
    time.sleep(3)  # let the payments limit refill
    before = sum(prometheus("rate_limited_total").values())
    with ThreadPoolExecutor(20) as pool:
        codes = list(pool.map(lambda _: call("GET", f"/payments/{pid}", token=student1)[0], range(120)))
    time.sleep(5)  # two scrapes
    counted = sum(prometheus("rate_limited_total").values()) - before
    assert codes.count(429) > 0 and set(codes) <= {200, 429}
    assert round(counted) == codes.count(429)
    time.sleep(3)  # refill again for whoever runs next
