"""Failure injection scenarios.

Usage (stack must be running): python3 experiments/inject.py <scenario> <baseline|ft>

Each fault lasts FAULT_S seconds and is then reverted (DB / node started again, bank back to normal),
except app_crash: the process is killed and only a restart policy (FT) brings it back.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = {"baseline": "uni-baseline", "ft": "uni-ft"}
SCENARIOS = ["app_crash", "db_failure", "network_timeout", "node_failure", "lost_transaction", "high_load"]
FAULT_S = 30


def sh(*cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def container(mode, service):
    # e.g. uni-ft-payment-service-1-1, uni-baseline-payment-service-1
    return f"{PROJECT[mode]}-{service}-1"


def bank_fault(mode, delay_s=0):
    req = urllib.request.Request(
        "http://localhost:8090/admin/fault",
        data=json.dumps({"mode": mode, "delay_s": delay_s}).encode(),
        headers={"content-type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=5).read()


def after(seconds, fn, *args):
    """Run fn(*args) in the background after `seconds`. Returns the thread."""
    def job():
        time.sleep(seconds)
        fn(*args)
    t = threading.Thread(target=job)
    t.start()
    return t


def node_b_containers(mode):
    return sh("docker", "ps", "-aq", "--filter", "label=node=b",
              "--filter", f"label=com.docker.compose.project={PROJECT[mode]}").split()


def run_locust(mode, run_id):
    subprocess.run([
        "docker", "run", "--rm", "--network", f"{PROJECT[mode]}_default",
        "-v", f"{ROOT}/experiments:/mnt/exp:ro", "-v", f"{ROOT}/results/raw:/mnt/out",
        "locustio/locust", "-f", "/mnt/exp/locustfile.py", "--headless",
        "-u", "200", "-r", "50", "-t", "60s", "--host", "http://gateway",
        "--csv", f"/mnt/out/{run_id}_locust", "--only-summary",
    ], capture_output=True)


def inject(scenario, mode, run_id="manual"):
    """Start the fault now. Returns a thread that ends when the fault is over (or None)."""
    if scenario == "app_crash":
        # Real crash: kill the app process inside the container. (docker kill counts as a
        # manual stop, so Docker would not apply the restart policy.)
        name = container(mode, "payment-service-1" if mode == "ft" else "payment-service")
        subprocess.run(["docker", "exec", name, "sh", "-c", "kill -9 -1"], capture_output=True)
        return None
    if scenario == "db_failure":
        name = container(mode, "postgres-primary")
        sh("docker", "stop", "-t", "0", name)
        return after(FAULT_S, sh, "docker", "start", name)
    if scenario == "network_timeout":
        bank_fault("delay", 5)
        return after(FAULT_S, bank_fault, "none")
    if scenario == "node_failure":
        ids = node_b_containers(mode)
        sh("docker", "stop", "-t", "0", *ids)
        return after(FAULT_S, sh, "docker", "start", *ids)
    if scenario == "lost_transaction":
        bank_fault("crash_after_debit")
        return after(FAULT_S, bank_fault, "none")
    if scenario == "high_load":
        t = threading.Thread(target=run_locust, args=(mode, run_id))
        t.start()
        return t
    raise ValueError(f"unknown scenario {scenario}, choose from {SCENARIOS}")


if __name__ == "__main__":
    scenario, mode = sys.argv[1], sys.argv[2]
    print(f"{time.time():.3f} inject {scenario} ({mode})")
    thread = inject(scenario, mode)
    if thread:
        thread.join()
    print(f"{time.time():.3f} fault over")
