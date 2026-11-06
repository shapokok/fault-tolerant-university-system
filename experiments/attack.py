"""Sends denied requests on purpose, for the alert demo and the Grafana screenshots (endterm stack).

  unauthorized  about 2 per second: no token (401) and student1 reading student 2 (403)
  ratelimit     about 40 payment reads per second from this machine; the limit is 20/s per IP,
                so about half are answered 429 by the gateway
Runs until Ctrl+C or --seconds. Prints the status counts every 10 s.
Usage: python3 experiments/attack.py unauthorized|ratelimit [--seconds N]
"""
import argparse
import collections
import json
import time
import urllib.error
import urllib.request
import uuid

import auth

G = "http://localhost:8080"


def call(method, path, token=None, body=None, headers=None):
    h = {"content-type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(G + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["unauthorized", "ratelimit"])
    ap.add_argument("--seconds", type=float, default=3600)
    args = ap.parse_args()

    token = auth.student_token(1)
    if args.mode == "ratelimit":  # a payment of student1, read again and again
        code, body = call("POST", "/payments", token, {"student_id": 1, "amount": 10}, {"Idempotency-Key": str(uuid.uuid4())})
        pid = json.loads(body)["payment_id"]
        requests, rate = [("GET", f"/payments/{pid}", token)], 40
    else:
        requests, rate = [("GET", "/students/1", None), ("GET", "/students/2", token)], 2

    counts, start, last_print, i = collections.Counter(), time.time(), time.time(), 0
    try:
        while time.time() - start < args.seconds:
            method, path, tok = requests[i % len(requests)]
            counts[call(method, path, tok)[0]] += 1
            i += 1
            time.sleep(max(0, start + i / rate - time.time()))
            if time.time() - last_print >= 10:
                print(f"{time.strftime('%H:%M:%S')} {args.mode}: {dict(counts)}", flush=True)
                last_print = time.time()
    except KeyboardInterrupt:
        pass
    print(f"total {args.mode}: {dict(counts)}", flush=True)


if __name__ == "__main__":
    main()
