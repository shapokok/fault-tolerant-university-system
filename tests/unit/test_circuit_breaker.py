"""ft.CircuitBreaker: closed -> open -> half_open -> closed or open."""
import asyncio

import pytest
from prometheus_client import REGISTRY

import ft
import obs


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(ft.time, "time", c)
    return c


async def fail():
    raise RuntimeError("dependency down")


async def ok():
    return "ok"


def call(breaker, fn):
    return asyncio.run(breaker.call(fn))


def test_opens_after_threshold(clock):
    """After `threshold` failed calls in a row the breaker is open."""
    b = ft.CircuitBreaker("dep-a", threshold=3)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            call(b, fail)
    assert b.state == "open"


def test_open_breaker_rejects_without_calling(clock):
    """While open, calls fail at once with CircuitOpen and the dependency is not called."""
    b = ft.CircuitBreaker("dep-b", threshold=1)
    with pytest.raises(RuntimeError):
        call(b, fail)
    calls = []

    async def tracked():
        calls.append(1)
        return "ok"
    with pytest.raises(ft.CircuitOpen):
        call(b, tracked)
    assert calls == []


def test_success_resets_the_failure_count(clock):
    """Failures must be in a row: a success in between keeps the breaker closed."""
    b = ft.CircuitBreaker("dep-c", threshold=3)
    for fn in (fail, fail, ok, fail, fail):
        try:
            call(b, fn)
        except RuntimeError:
            pass
    assert b.state == "closed"


def test_half_open_trial_success_closes(clock):
    """After reset_timeout_s one trial call is allowed; if it succeeds the breaker closes."""
    b = ft.CircuitBreaker("dep-d", threshold=1, reset_timeout_s=10)
    with pytest.raises(RuntimeError):
        call(b, fail)
    clock.now += 9.9
    with pytest.raises(ft.CircuitOpen):
        call(b, ok)
    clock.now += 0.2
    assert call(b, ok) == "ok"
    assert b.state == "closed"


def test_half_open_trial_failure_reopens(clock):
    """If the trial call fails, the breaker opens again for another reset_timeout_s."""
    b = ft.CircuitBreaker("dep-e", threshold=1, reset_timeout_s=10)
    with pytest.raises(RuntimeError):
        call(b, fail)
    clock.now += 11
    with pytest.raises(RuntimeError):
        call(b, fail)
    assert b.state == "open"
    with pytest.raises(ft.CircuitOpen):
        call(b, ok)


def test_only_one_trial_at_a_time(clock):
    """In half_open a second call made while the trial is still running is rejected."""
    b = ft.CircuitBreaker("dep-f", threshold=1, reset_timeout_s=10)
    with pytest.raises(RuntimeError):
        call(b, fail)
    clock.now += 11

    async def scenario():
        release = asyncio.Event()

        async def slow():
            await release.wait()
            return "ok"
        trial = asyncio.create_task(b.call(slow))
        await asyncio.sleep(0)  # the trial is now running
        with pytest.raises(ft.CircuitOpen):
            await b.call(ok)
        release.set()
        return await trial
    assert asyncio.run(scenario()) == "ok"
    assert b.state == "closed"


def test_state_is_exported_as_metric(clock):
    """circuit_breaker_state follows the breaker: 2 when open, 0 when closed again."""
    b = ft.CircuitBreaker("dep-g", threshold=1, reset_timeout_s=10)
    with pytest.raises(RuntimeError):
        call(b, fail)
    labels = {"service": obs.SERVICE, "target": "dep-g"}
    assert REGISTRY.get_sample_value("circuit_breaker_state", labels) == 2
    clock.now += 11
    call(b, ok)
    assert REGISTRY.get_sample_value("circuit_breaker_state", labels) == 0
