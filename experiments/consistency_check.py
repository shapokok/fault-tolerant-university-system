"""Checks that money and transcripts are correct after a run.

Usage (stack must be running): python3 experiments/consistency_check.py <baseline|ft>
"""
import json
import subprocess
import sys
import urllib.request
from collections import Counter
from decimal import Decimal

from inject import ROOT, container


def psql(mode, query):
    return subprocess.run(
        ["docker", "compose", "-f", f"docker-compose.{mode}.yml", "exec", "-T", "postgres-primary",
         "psql", "-U", "uni", "-d", "uni", "-t", "-A", "-F", ",", "-c", query],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def reconcile(mode):
    """FT only: resolve pending payments now instead of waiting for the background job."""
    code = "import httpx; print(httpx.post('http://localhost:8000/admin/reconcile', timeout=60).text)"
    for name in ("payment-service-1", "payment-service-2"):
        r = subprocess.run(["docker", "exec", container(mode, name), "python", "-c", code],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    return "reconcile failed"


def check(mode):
    """Returns (passed, reason)."""
    if mode == "ft":
        reconcile(mode)
    problems = []

    dup = int(psql(mode, "SELECT count(*) FROM (SELECT idempotency_key FROM payments "
                         "WHERE status = 'completed' GROUP BY 1 HAVING count(*) > 1) d"))
    if dup:
        problems.append(f"{dup} payments completed more than once")

    ledger, completed = psql(mode, "SELECT (SELECT coalesce(sum(amount), 0) FROM ledger), "
                                   "(SELECT coalesce(sum(amount), 0) FROM payments WHERE status = 'completed')").split(",")
    if Decimal(ledger) != Decimal(completed):
        problems.append(f"ledger total {ledger} != completed payments {completed}")

    pending = int(psql(mode, "SELECT count(*) FROM payments WHERE status = 'pending'"))
    if pending:
        problems.append(f"{pending} payments still pending")

    charges = json.load(urllib.request.urlopen("http://localhost:8090/charges", timeout=10))
    per_key = Counter(c["ref"] for c in charges)
    double = sum(1 for n in per_key.values() if n > 1)
    if double:
        problems.append(f"{double} keys charged more than once by the bank")
    completed_keys = set(psql(mode, "SELECT DISTINCT idempotency_key FROM payments WHERE status = 'completed'").split())
    lost = sum(1 for key in per_key if key not in completed_keys)
    if lost:
        problems.append(f"{lost} bank charges without a completed payment")

    job = psql(mode, "SELECT j.status, (SELECT count(*) FROM transcripts t WHERE t.job_id = j.id), "
                     "(SELECT count(*) FROM students) FROM transcript_jobs j ORDER BY j.id DESC LIMIT 1")
    if job:
        status, done, students = job.split(",")
        if status != "done" or done != students:
            problems.append(f"transcript job {status}, {done}/{students} transcripts")

    return not problems, "; ".join(problems) or "all checks passed"


if __name__ == "__main__":
    passed, reason = check(sys.argv[1])
    print("PASS" if passed else "FAIL", "-", reason)
