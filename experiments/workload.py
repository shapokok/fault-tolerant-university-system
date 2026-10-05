"""Steady async load against the gateway. Logs every request to a CSV file.

Usage: python workload.py --rate 20 --duration 30 --out /results/raw/smoke.csv
"""
import argparse
import asyncio
import csv
import random
import statistics
import time
import uuid

import httpx

MIX = {"timetable": 40, "student": 20, "payment": 20, "registration": 10, "transcript": 10}
N_STUDENTS, N_COURSES = 50, 10
RESEND_SHARE = 0.1  # share of payments that deliberately reuse an old idempotency key

rows = []
sent_keys = []


def build_request(kind):
    sid = random.randint(1, N_STUDENTS)
    if kind == "timetable":
        return "GET", f"/timetable/{sid}", None, {}, ""
    if kind == "student":
        return "GET", f"/students/{sid}", None, {}, ""
    if kind == "transcript":
        return "GET", f"/transcripts/{sid}", None, {}, ""
    if kind == "registration":
        return "POST", "/registrations", {"student_id": sid, "course_id": random.randint(1, N_COURSES)}, {}, ""
    # payment
    if sent_keys and random.random() < RESEND_SHARE:
        key, resent = random.choice(sent_keys), "1"
    else:
        key, resent = str(uuid.uuid4()), "0"
        sent_keys.append(key)
    return "POST", "/payments", {"student_id": sid, "amount": 100}, {"Idempotency-Key": key}, resent


async def one_request(client, kind):
    method, path, body, headers, resent = build_request(kind)
    start = time.time()
    try:
        r = await client.request(method, path, json=body, headers=headers)
        status, attempts = r.status_code, r.headers.get("X-Attempts", "1")
    except httpx.HTTPError:
        status, attempts = 0, "1"  # no response at all (connection error or client timeout)
    rows.append({
        "ts": round(start, 3),
        "endpoint": kind,
        "status": status,
        "latency_ms": round((time.time() - start) * 1000, 1),
        "attempts": attempts,
        "idempotency_key": headers.get("Idempotency-Key", ""),
        "resent": resent,
    })


async def run(base_url, rate, duration):
    kinds, weights = list(MIX), list(MIX.values())
    tasks = []
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        start = next_t = time.time()
        print(f"workload started {start:.3f}", flush=True)
        while time.time() - start < duration:
            kind = random.choices(kinds, weights)[0]
            tasks.append(asyncio.create_task(one_request(client, kind)))
            next_t += 1 / rate
            await asyncio.sleep(max(0, next_t - time.time()))
        await asyncio.gather(*tasks)


def summary():
    print(f"{'endpoint':<14}{'total':>7}{'ok %':>8}{'p50 ms':>9}{'p95 ms':>9}")
    for kind in list(MIX) + ["ALL"]:
        sel = [r for r in rows if kind == "ALL" or r["endpoint"] == kind]
        if not sel:
            continue
        ok = sum(1 for r in sel if 200 <= r["status"] < 300)
        lat = sorted(r["latency_ms"] for r in sel)
        p95 = lat[int(0.95 * (len(lat) - 1))]
        print(f"{kind:<14}{len(sel):>7}{100 * ok / len(sel):>8.1f}{statistics.median(lat):>9.1f}{p95:>9.1f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://gateway")
    ap.add_argument("--rate", type=float, default=20)
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--out", default="/results/raw/workload.csv")
    args = ap.parse_args()

    asyncio.run(run(args.base_url, args.rate, args.duration))
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["ts"]))
    print(f"wrote {len(rows)} rows to {args.out}")
    summary()
