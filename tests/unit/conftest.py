"""Unit tests import the service modules directly, with security on and a test-only secret."""
import os
import sys

os.environ.setdefault("SECURITY_MODE", "on")
os.environ.setdefault("JWT_SECRET", "unit-test-only-secret-" + "x" * 40)  # not a real secret
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services"))
