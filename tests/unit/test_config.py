"""Parses gateway/nginx.ft.conf: the rate limits and header rules are what the docs say."""
import os
import re

import gateway_exporter

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
FT = open(os.path.join(ROOT, "gateway", "nginx.ft.conf")).read()
BASELINE = open(os.path.join(ROOT, "gateway", "nginx.baseline.conf")).read()


def location(path):
    m = re.search(r"location\s+(?:=\s+)?" + re.escape(path) + r"\s*\{([^}]*)\}", FT)
    assert m, f"no location {path}"
    return m.group(1)


def test_rate_limit_zones():
    """Zones per client IP: login 5 requests/s, payments 20 requests/s."""
    zones = dict(re.findall(r"limit_req_zone\s+\$binary_remote_addr\s+zone=(\w+):\S+\s+rate=(\S+);", FT))
    assert zones == {"login": "5r/s", "payments": "20r/s"}


def test_limits_are_applied_to_the_right_routes():
    """/auth/login uses zone login with burst 10, /payments zone payments with burst 40, both nodelay."""
    assert re.search(r"limit_req\s+zone=login\s+burst=10\s+nodelay;", location("/auth/login"))
    assert re.search(r"limit_req\s+zone=payments\s+burst=40\s+nodelay;", location("/payments"))


def test_rejections_are_429_and_logged_for_the_exporter():
    """Rejected requests get 429, and 429 lines are written to the file gateway-exporter reads."""
    assert re.search(r"limit_req_status\s+429;", FT)
    assert "access_log /var/log/gateway/rate_limited.json json_access if=$rate_limited;" in FT


def test_header_rules():
    """The gateway removes X-Probe, sets X-Real-IP from the real client and limits bodies to 16 KB."""
    assert re.search(r'proxy_set_header\s+X-Probe\s+"";', FT)
    assert re.search(r"proxy_set_header\s+X-Real-IP\s+\$remote_addr;", FT)
    assert re.search(r"client_max_body_size\s+16k;", FT)


def test_internal_endpoints_not_routed():
    """No location routes /admin or /metrics through the gateway."""
    assert not re.search(r"location\s+[=~^]*\s*/(admin|metrics)", FT)


def test_baseline_has_no_rate_limits():
    """The baseline gateway (midterm) has no rate limiting."""
    assert "limit_req" not in BASELINE


def test_exporter_route_names():
    """gateway-exporter maps request paths to the route label."""
    assert gateway_exporter.route("/auth/login") == "/auth/login"
    assert gateway_exporter.route("/payments/17") == "/payments"
    assert gateway_exporter.route("/students/1") == "other"
