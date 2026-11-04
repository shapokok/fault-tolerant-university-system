"""Checks the observability stack on the running system. Exit code 1 if anything fails.

Usage: python3 experiments/check_observability.py <baseline|ft>
Checks: Prometheus targets up, every service log line is JSON with the required fields,
Grafana loads the dashboard anonymously, every dashboard query is valid.
"""
import json
import subprocess
import sys
import urllib.parse
import urllib.request

from inject import ROOT

SERVICES = {
    "baseline": ["student-service", "transcript-service", "payment-service", "mock-bank"],
    "ft": [f"{k}-service-{n}" for k in ("student", "transcript", "payment") for n in (1, 2)] + ["mock-bank"],
}
LOG_FIELDS = {"ts", "level", "service", "instance", "request_id", "method", "path", "status",
              "latency_ms", "user_id", "event", "message"}
failed = 0


def report(ok, text):
    global failed
    failed += not ok
    print("OK  " if ok else "FAIL", text)


def get_json(url):
    return json.load(urllib.request.urlopen(url, timeout=5))


def check_targets(mode):
    targets = get_json("http://localhost:9090/api/v1/targets")["data"]["activeTargets"]
    up = {t["labels"]["instance"] for t in targets if t["health"] == "up"}
    missing = set(SERVICES[mode]) - up
    report(not missing, f"Prometheus targets up: {len(up)}/{len(SERVICES[mode])} {sorted(missing) or ''}")


def check_logs(mode):
    for service in SERVICES[mode]:
        out = subprocess.run(["docker", "compose", "-f", f"docker-compose.{mode}.yml", "logs", "--no-log-prefix",
                              "--no-color", service], cwd=ROOT, capture_output=True, text=True).stdout
        lines = [line for line in out.splitlines() if line.strip()]
        bad = 0
        for line in lines:
            try:
                bad += not LOG_FIELDS <= set(json.loads(line))
            except ValueError:
                bad += 1
        report(lines and not bad, f"{service}: {len(lines)} log lines, {bad} not valid")


def check_grafana():
    dash = get_json("http://localhost:3000/api/dashboards/uid/university")["dashboard"]  # no login
    report(True, f"Grafana anonymous access, dashboard '{dash['title']}' with {len(dash['panels'])} panels")
    sources = get_json("http://localhost:3000/api/datasources/uid/prometheus/health")
    report(sources.get("status") == "OK", f"Grafana datasource: {sources.get('message')}")
    for panel in dash["panels"]:
        for target in panel["targets"]:
            query = urllib.parse.urlencode({"query": target["expr"]})
            result = get_json(f"http://localhost:9090/api/v1/query?{query}")
            n = len(result["data"]["result"])
            report(result["status"] == "success", f"panel '{panel['title']}': {n} series")


if __name__ == "__main__":
    mode = sys.argv[1]
    check_targets(mode)
    check_logs(mode)
    check_grafana()
    sys.exit(1 if failed else 0)
