"""Metrics from the request log and the monitor log (definitions from CLAUDE.md)."""
import bisect
import csv
import os


def load_requests(path):
    """Rows sorted by time: (ts, ok, attempts)."""
    with open(path) as f:
        rows = [(float(r["ts"]), r["status"].startswith("2"), int(r["attempts"] or 1)) for r in csv.DictReader(f)]
    return sorted(rows)


def counts(rows):
    total = len(rows)
    ok = sum(1 for _, good, _ in rows if good)
    recovered = sum(1 for _, good, attempts in rows if good and attempts > 1)  # succeeded after >= 1 retry
    return {"total": total, "successful": ok, "failed": total - ok, "recovered": recovered,
            "availability": round(ok / total, 4) if total else None}


def detection_time(monitor_path, t_inject, t_until=None):
    """Injection -> first non-'up' state in the monitor log (breaker open shows up as 'degraded')."""
    if not os.path.exists(monitor_path):
        return None
    with open(monitor_path) as f:
        times = [float(r["ts"]) for r in csv.DictReader(f)
                 if r["state"] != "up" and float(r["ts"]) >= t_inject and (t_until is None or float(r["ts"]) < t_until)]
    return round(min(times) - t_inject, 2) if times else None


def recovery_time(rows, t_inject, t_until=None, window=10.0, step=0.5, target=0.99):
    """Injection -> start of the first 10 s window with >= 99% success. None = not recovered."""
    ts = [r[0] for r in rows]
    end = min(ts[-1], t_until) if t_until else ts[-1]
    start = t_inject
    while start + window <= end:
        lo, hi = bisect.bisect_left(ts, start), bisect.bisect_left(ts, start + window)
        sel = rows[lo:hi]
        if sel and sum(1 for _, good, _ in sel if good) / len(sel) >= target:
            return round(start - t_inject, 2)
        start += step
    return None


def p95_latency(path):
    with open(path) as f:
        lat = sorted(float(r["latency_ms"]) for r in csv.DictReader(f))
    return lat[int(0.95 * (len(lat) - 1))] if lat else None
