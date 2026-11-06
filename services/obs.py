"""Observability: Prometheus metrics, JSON logs, correlation ID.

On in both modes. It measures the system, it is not a resilience mechanism.
"""
import contextvars
import json
import logging
import re
import socket
import sys
import time
import uuid

from prometheus_client import Counter, Gauge, Histogram, disable_created_metrics

disable_created_metrics()  # keep /metrics short: no extra *_created series

SERVICE = "unknown"  # set once by setup()
INSTANCE = socket.gethostname()  # compose sets hostname, e.g. payment-service-1

# ---------- metrics ----------

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests handled",
                        ["service", "instance", "endpoint", "method", "status", "source"])
# Prometheus can only estimate a quantile inside a bucket, so bucket edges must be close to the
# latencies we cause. Default edges plus: 1 ms and 2.5 ms (fast services take about 1 ms), and
# 1.1, 1.25, 1.5, 2 and 3 s (a 1 s bank delay was shown as 2.4 s with only the 1 s and 2.5 s edges).
# 0.25, 0.5 and 0.75 are unchanged, so the HighLatencyP95 rule (500 ms) works exactly as before.
BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75,
           1.0, 1.1, 1.25, 1.5, 2.0, 2.5, 3.0, 5.0, 7.5, 10.0)
HTTP_DURATION = Histogram("http_request_duration_seconds", "HTTP request duration",
                          ["service", "endpoint", "source"], buckets=BUCKETS)
BREAKER_STATE = Gauge("circuit_breaker_state", "0 closed, 1 half_open, 2 open", ["service", "target"])
PAYMENTS = Counter("payments_total", "Payment events (a reconciled payment counts as pending, then completed)",
                   ["status"])
AUTH_FAILURES = Counter("auth_failures_total", "Failed authentications (used from stage E3)", ["reason"])
RATE_LIMITED = Counter("rate_limited_total", "Rate limited requests (used from stage E3)")

BREAKER_VALUES = {"closed": 0, "half_open": 1, "open": 2}


def set_breaker_state(target, state):
    BREAKER_STATE.labels(service=SERVICE, target=target).set(BREAKER_VALUES[state])


# ---------- correlation ID ----------

request_id = contextvars.ContextVar("request_id", default=None)
user_id = contextvars.ContextVar("user_id", default=None)  # filled from stage E3
VALID_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def read_request_id(header):
    """The ID nginx set, or a new one if the service was called directly or the value looks wrong."""
    return header if header and VALID_ID.match(header) else uuid.uuid4().hex


def outgoing_headers():
    """Headers for every outgoing HTTP call, so the next service logs the same request_id."""
    rid = request_id.get()
    return {"X-Request-ID": rid} if rid else {}


# ---------- JSON logs ----------

class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "service": SERVICE,
            "instance": INSTANCE,
            "request_id": request_id.get(),
            "method": None,
            "path": None,
            "status": None,
            "latency_ms": None,
            "user_id": user_id.get(),
            "event": "log",
            "message": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


logger = logging.getLogger("app")


def setup(service_name):
    """Send everything (ours and uvicorn's) to stdout as one JSON object per line."""
    global SERVICE
    SERVICE = service_name
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logging.captureWarnings(True)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # our middleware logs each request
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs every call at INFO


def log(event, message="", level=logging.INFO, exc_info=False, **fields):
    logger.log(level, message, exc_info=exc_info, extra={"fields": {"event": event, **fields}})


# ---------- one call per finished HTTP request ----------

# Polled all the time by the monitor, Docker and Prometheus: counted, but not logged.
NOT_LOGGED = {"/health", "/ready", "/metrics"}


def record_request(method, path, endpoint, status, seconds, source="user"):
    """source is "probe" for calls made by a /ready check (header X-Probe: 1), else "user"."""
    HTTP_REQUESTS.labels(service=SERVICE, instance=INSTANCE, endpoint=endpoint, method=method,
                         status=str(status), source=source).inc()
    HTTP_DURATION.labels(service=SERVICE, endpoint=endpoint, source=source).observe(seconds)
    if path in NOT_LOGGED or source == "probe":
        return
    level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
    log("http_request", f"{method} {path} {status}", level=level, method=method, path=path,
        status=status, latency_ms=round(seconds * 1000, 1))


def now():
    return time.perf_counter()
