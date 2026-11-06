"""Login helper for the scripts on this machine (endterm stack, SECURITY_MODE=on).

Secrets are read from .env (never committed). Only the endterm stack (mode "ft") uses login.
"""
import json
import os
import time
import urllib.error
import urllib.request

from inject import ROOT

GATEWAY = "http://localhost:8080"


def secure(mode):
    return mode == "ft"


def env(name):
    with open(os.path.join(ROOT, ".env")) as f:
        for line in f:
            key, _, value = line.strip().partition("=")
            if key == name and not line.startswith("#"):
                return value
    raise KeyError(f"{name} is missing in .env (copy .env.example)")


def login(username, password):
    """Returns a token. Waits and tries again when the login rate limit answers 429."""
    body = json.dumps({"username": username, "password": password}).encode()
    for _ in range(30):
        req = urllib.request.Request(GATEWAY + "/auth/login", data=body, method="POST",
                                     headers={"content-type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(req, timeout=10))["access_token"]
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(1)
    raise RuntimeError("login kept getting 429")


def student_token(sid=1):
    return login(f"student{sid}", env("STUDENT_PASSWORD"))


def admin_token():
    return login("admin", env("ADMIN_PASSWORD"))
