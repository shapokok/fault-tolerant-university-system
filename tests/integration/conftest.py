"""Integration tests run on this machine against the running endterm stack (docker-compose.ft.yml).
They reuse the helpers of the experiment scripts (login via .env, HTTP calls, psql, Prometheus)."""
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "experiments"))

import auth  # noqa: E402


@pytest.fixture(scope="session")
def student1():
    return auth.student_token(1)


@pytest.fixture(scope="session")
def student2():
    return auth.student_token(2)


@pytest.fixture(scope="session")
def admin():
    return auth.admin_token()
