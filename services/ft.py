"""Software fault tolerance helpers: timeouts, retry with backoff, circuit breaker.

Used only when FT_MODE=on (see common.py and the services).
"""
import asyncio
import contextvars
import random
import time

import httpx

HTTP_TIMEOUT_S = 2.0   # every outgoing HTTP call
DB_TIMEOUT_S = 2.0     # every DB connect and query
MAX_ATTEMPTS = 3
BASE_DELAY_S = 0.1     # backoff: 0.1 s, then 0.2 s (+ random jitter)

# Per-request retry counter, returned to the client as the X-Attempts header.
retry_box = contextvars.ContextVar("retry_box", default=None)


async def retry(fn, is_transient):
    """Call fn(). On a transient error wait (exponential backoff + jitter) and try again."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await fn()
        except Exception as e:
            if attempt == MAX_ATTEMPTS or not is_transient(e):
                raise
            box = retry_box.get()
            if box is not None:
                box["retries"] += 1
            delay = BASE_DELAY_S * 2 ** (attempt - 1)
            await asyncio.sleep(delay + random.uniform(0, delay))  # jitter spreads retries out


def is_transient_http(e):
    """Network problem or 502/503/504: worth another try (for safe, read-only calls)."""
    if isinstance(e, httpx.TransportError):
        return True
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (502, 503, 504)


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    """closed: calls pass. After `threshold` failed calls in a row -> open: calls fail at once.
    After `reset_timeout_s` -> half_open: one trial call. Success -> closed, failure -> open."""

    def __init__(self, name, threshold=5, reset_timeout_s=10):
        self.name = name
        self.threshold = threshold
        self.reset_timeout_s = reset_timeout_s
        self.state = "closed"
        self.failures = 0
        self.opened_at = 0.0
        self.trial_running = False

    def _set(self, state):
        if state != self.state:
            print(f"{time.time():.3f} breaker {self.name}: {self.state} -> {state}", flush=True)
            self.state = state
        if state == "open":
            self.opened_at = time.time()
        self.trial_running = False

    async def call(self, fn):
        if self.state == "open":
            if time.time() - self.opened_at < self.reset_timeout_s:
                raise CircuitOpen(self.name)
            self._set("half_open")
        if self.state == "half_open":
            if self.trial_running:
                raise CircuitOpen(self.name)  # only one trial call at a time
            self.trial_running = True

        try:
            result = await fn()
        except Exception:
            self.failures += 1
            if self.state == "half_open" or self.failures >= self.threshold:
                self._set("open")
            raise
        self.failures = 0
        self._set("closed")
        return result
