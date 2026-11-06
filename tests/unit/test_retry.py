"""ft.retry and ft.is_transient_http."""
import asyncio

import httpx
import pytest

import ft


class Flaky:
    """Fails `failures` times with `error`, then returns "ok"."""

    def __init__(self, failures, error):
        self.failures, self.error, self.calls = failures, error, 0

    async def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "ok"


@pytest.fixture
def sleeps(monkeypatch):
    """Records backoff delays instead of waiting."""
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(ft.asyncio, "sleep", fake_sleep)
    return delays


def status_error(code):
    request = httpx.Request("GET", "http://test")
    return httpx.HTTPStatusError("test", request=request, response=httpx.Response(code, request=request))


def test_recovers_from_transient_errors(sleeps):
    """A call that fails twice with a transient error (connection refused) succeeds on attempt 3."""
    call = Flaky(2, httpx.ConnectError("refused"))
    assert asyncio.run(ft.retry(call, ft.is_transient_http)) == "ok"
    assert call.calls == 3


def test_gives_up_after_three_attempts(sleeps):
    """A call that keeps failing is tried exactly 3 times, then the error is raised."""
    call = Flaky(10, httpx.ConnectError("refused"))
    with pytest.raises(httpx.ConnectError):
        asyncio.run(ft.retry(call, ft.is_transient_http))
    assert call.calls == ft.MAX_ATTEMPTS == 3


def test_no_retry_for_permanent_errors(sleeps):
    """A non-transient error (HTTP 500) is not retried: 1 call, no waiting."""
    call = Flaky(10, status_error(500))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(ft.retry(call, ft.is_transient_http))
    assert call.calls == 1 and sleeps == []


def test_backoff_doubles_with_jitter(sleeps):
    """Waits grow exponentially with random jitter: first 0.1 to 0.2 s, second 0.2 to 0.4 s."""
    asyncio.run(ft.retry(Flaky(2, httpx.ConnectError("refused")), ft.is_transient_http))
    assert len(sleeps) == 2
    assert 0.1 <= sleeps[0] <= 0.2 and 0.2 <= sleeps[1] <= 0.4


def test_retries_are_counted_for_the_response_header(sleeps):
    """Every retry is added to the per-request counter that becomes the X-Attempts header."""
    async def run():
        box = {"retries": 0}
        ft.retry_box.set(box)
        await ft.retry(Flaky(2, httpx.ConnectError("refused")), ft.is_transient_http)
        return box["retries"]
    assert asyncio.run(run()) == 2


@pytest.mark.parametrize("error, transient", [
    (httpx.ConnectError("refused"), True),
    (httpx.ReadTimeout("slow"), True),
    (status_error(502), True),
    (status_error(503), True),
    (status_error(504), True),
    (status_error(500), False),
    (status_error(404), False),
    (ValueError("bug"), False),
], ids=["connect error", "read timeout", "502", "503", "504", "500", "404", "other exception"])
def test_transient_classification(error, transient):
    """Only network errors and 502/503/504 count as transient (worth a retry)."""
    assert ft.is_transient_http(error) is transient
