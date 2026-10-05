"""Polls /health and /ready of every instance every 0.5 s and logs state changes to CSV.

States: up (healthy, primary DB), degraded (alive but DB on replica or not ready), down (no answer).
MONITOR_TARGETS="name=url,name=url,..."
"""
import asyncio
import csv
import os
import time

import httpx

TARGETS = dict(t.split("=", 1) for t in os.environ["MONITOR_TARGETS"].split(","))
OUT = os.getenv("MONITOR_OUT", "/results/monitor.csv")
INTERVAL = 0.5


async def check(client, url):
    try:
        h = await client.get(url + "/health")
        if h.status_code != 200:
            return "down", f"health {h.status_code}"
    except Exception as e:
        return "down", type(e).__name__
    try:
        r = await client.get(url + "/ready")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            return "degraded", body.get("reason") or f"ready {r.status_code}"
        if body.get("db") == "replica":
            return "degraded", "db on replica"
    except Exception as e:  # never let one odd answer kill the monitor
        return "degraded", "ready " + type(e).__name__
    return "up", ""


async def main():
    state = {}
    new_file = not os.path.exists(OUT)
    with open(OUT, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["ts", "target", "state", "detail"])
        async with httpx.AsyncClient(timeout=0.4) as client:
            while True:
                t0 = time.time()
                results = await asyncio.gather(*(check(client, u) for u in TARGETS.values()))
                for name, (s, detail) in zip(TARGETS, results):
                    if state.get(name) != s:
                        state[name] = s
                        w.writerow([round(t0, 3), name, s, detail])
                        f.flush()
                        print(f"{t0:.3f} {name} -> {s} {detail}", flush=True)
                await asyncio.sleep(max(0, INTERVAL - (time.time() - t0)))


asyncio.run(main())
