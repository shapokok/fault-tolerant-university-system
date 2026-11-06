"""Tokens (create, verify, refuse) and the authorization rules in security.py."""
import asyncio
import time

import jwt
import pytest
from fastapi import HTTPException
from prometheus_client import REGISTRY
from starlette.requests import Request

import security


@pytest.fixture(autouse=True)
def no_audit_db(monkeypatch):
    """Unit tests have no database: record audit events instead of writing them."""
    events = []

    async def fake_audit(request, action, resource, result, user_id=None):
        events.append((action, result))
    monkeypatch.setattr(security, "audit", fake_audit)
    return events


def request(authorization=None, path="/students/1"):
    headers = [(b"authorization", authorization.encode())] if authorization else []
    return Request({"type": "http", "method": "GET", "path": path, "headers": headers, "query_string": b"",
                    "client": ("10.0.0.9", 5000), "server": ("test", 80), "scheme": "http", "root_path": ""})


def verify(token):
    return asyncio.run(security.current_user(request("Bearer " + token if token is not None else None)))


def refused_with(token):
    with pytest.raises(HTTPException) as e:
        verify(token)
    return e.value.status_code


def forge(claims, secret=None, algorithm="HS256"):
    return jwt.encode(claims, secret or security.JWT_SECRET, algorithm=algorithm)


def test_token_round_trip():
    """A token from create_token is accepted and carries user, role and student id; it lives 15 min."""
    claims = verify(security.create_token("student7", "student", 7))
    assert (claims["sub"], claims["role"], claims["sid"]) == ("student7", "student", 7)
    assert claims["exp"] - claims["iat"] == 15 * 60


def test_missing_token_is_refused():
    """No Authorization header, or not a Bearer token: 401."""
    assert refused_with(None) == 401
    with pytest.raises(HTTPException) as e:
        asyncio.run(security.current_user(request("Basic dXNlcjpwYXNz")))
    assert e.value.status_code == 401


def test_expired_token_is_refused():
    """A correctly signed token whose exp is in the past: 401."""
    now = int(time.time())
    assert refused_with(forge({"sub": "student1", "role": "student", "sid": 1, "iat": now - 1000, "exp": now - 60})) == 401


def test_token_with_another_secret_is_refused():
    """A token signed with another secret (forged admin): 401."""
    assert refused_with(forge({"sub": "admin", "role": "admin", "exp": int(time.time()) + 600}, secret="guess-" * 8)) == 401


def test_alg_none_token_is_refused():
    """A token with alg none (no signature) is refused: the algorithm is fixed to HS256."""
    assert refused_with(jwt.encode({"sub": "admin", "role": "admin", "exp": int(time.time()) + 600}, None, algorithm="none")) == 401


def test_token_without_role_is_refused():
    """exp, sub and role are required claims: a token without role gets 401."""
    assert refused_with(forge({"sub": "student1", "exp": int(time.time()) + 600})) == 401


def test_refusals_are_counted_and_audited(no_audit_db):
    """Every refusal adds 1 to auth_failures_total{reason} and writes an audit event."""
    before = REGISTRY.get_sample_value("auth_failures_total", {"reason": "expired_token"})
    now = int(time.time())
    refused_with(forge({"sub": "s", "role": "student", "exp": now - 1}))
    assert REGISTRY.get_sample_value("auth_failures_total", {"reason": "expired_token"}) == before + 1
    assert ("access", "denied: expired_token") in no_audit_db


def test_service_token():
    """A service token has role service and lives only 60 s."""
    claims = verify(security.service_auth("transcript-service")[len("Bearer "):])
    assert claims["role"] == "service" and claims["exp"] - claims["iat"] == 60


STUDENT1 = {"sub": "student1", "role": "student", "sid": 1}
ADMIN = {"sub": "admin", "role": "admin"}
SERVICE = {"sub": "transcript-service", "role": "service"}


def allowed(check, *args):
    try:
        asyncio.run(check(request(), *args))
        return True
    except HTTPException as e:
        assert e.status_code == 403
        return False


@pytest.mark.parametrize("user, student, write, expected", [
    (STUDENT1, 1, False, True),   # own data
    (STUDENT1, 1, True, True),
    (STUDENT1, 2, False, False),  # another student
    (STUDENT1, 2, True, False),
    (ADMIN, 2, False, True),      # admin reads everyone
    (ADMIN, 2, True, False),      # but does not pay or register for students
    (SERVICE, 2, False, True),    # batch job reads
    (SERVICE, 2, True, False),
], ids=["student reads own", "student writes own", "student reads other", "student writes other",
        "admin reads", "admin writes", "service reads", "service writes"])
def test_student_data_rules(user, student, write, expected):
    """Students: only their own data. Admin and services: read any student. Writes: only the student."""
    assert allowed(security.check_student, user, student, write) is expected


@pytest.mark.parametrize("user, expected", [(ADMIN, True), (STUDENT1, False), (SERVICE, False)],
                         ids=["admin", "student", "service"])
def test_admin_only(user, expected):
    """Admin endpoints accept only role admin (403 for students and services)."""
    assert allowed(security.check_admin, user) is expected
