"""Authentication, authorization, input limits and audit log (stage E3).

Active only when SECURITY_MODE=on (endterm stack). With SECURITY_MODE=off every check here
does nothing, so the baseline keeps its midterm behavior.

Token: JWT signed with HS256, valid 15 minutes. Claims: sub (username), role (student, admin
or service) and sid (student id, students only). Every service checks the token itself.
"""
import asyncio
import logging
import os
import time

import asyncpg
import bcrypt
import jwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import obs

ON = os.getenv("SECURITY_MODE", "off") == "on"
JWT_SECRET = os.getenv("JWT_SECRET", "")
ALGORITHM = "HS256"
TOKEN_TTL_S = 15 * 60
MAX_FAILED_LOGINS = 5   # failures in a row ...
LOCK_S = 60             # ... lock the account for this long
N_STUDENTS, N_COURSES = 50, 10  # seeded range: ids outside it are invalid input (422)

if ON and len(JWT_SECRET) < 32:
    raise RuntimeError("SECURITY_MODE=on needs JWT_SECRET from .env (at least 32 characters)")

# Checked when the username does not exist, so a wrong username takes as long as a wrong
# password: an attacker cannot find valid usernames by timing the answer.
DUMMY_HASH = bcrypt.hashpw(b"not-a-password", bcrypt.gensalt(10))

REASONS = ("missing_token", "invalid_token", "expired_token", "forbidden", "bad_password", "unknown_user", "locked")
for _reason in REASONS:
    obs.AUTH_FAILURES.labels(reason=_reason)  # start every series at 0, so the first failure is counted


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_]+$")
    password: str = Field(min_length=1, max_length=72)  # bcrypt uses at most 72 bytes


# ---------- tokens ----------

def create_token(username, role, student_id=None, ttl_s=TOKEN_TTL_S):
    now = int(time.time())
    claims = {"sub": username, "role": role, "iat": now, "exp": now + ttl_s}
    if student_id is not None:
        claims["sid"] = student_id
    return jwt.encode(claims, JWT_SECRET, algorithm=ALGORITHM)


def service_auth(name):
    """Authorization header for work a service does on its own (the batch job), not for a user."""
    return "Bearer " + create_token(name, "service", ttl_s=60)


async def current_user(request: Request):
    """FastAPI dependency: the checked token claims, or None when security is off."""
    if not ON:
        return None
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        await deny(request, 401, "missing_token")
    try:
        # algorithms is fixed: a token that says "alg: none" or another algorithm is rejected.
        claims = jwt.decode(header[7:], JWT_SECRET, algorithms=[ALGORITHM],
                            options={"require": ["exp", "sub", "role"]})
    except jwt.ExpiredSignatureError:
        await deny(request, 401, "expired_token")
    except jwt.InvalidTokenError:
        await deny(request, 401, "invalid_token")
    obs.user_id.set(claims["sub"])
    return claims


# ---------- authorization (least privilege) ----------

async def check_student(request, user, student_id, write=False):
    """A student may read and change only their own data. Admin and services may read any
    student. Writes (pay, register) are allowed only to the student: not even the admin."""
    if not ON:
        return
    if user["role"] == "student" and user.get("sid") == student_id:
        return
    if not write and user["role"] in ("admin", "service"):
        return
    await deny(request, 403, "forbidden")


async def check_admin(request, user):
    if ON and user["role"] != "admin":
        await deny(request, 403, "forbidden")


async def deny(request, status, reason, action="access", detail=None, user_id=None):
    """Count, write to the audit log and refuse. 401 = not authenticated, 403 = not allowed."""
    obs.AUTH_FAILURES.labels(reason=reason).inc()
    await audit(request, action, request.url.path, f"denied: {reason}", user_id=user_id)
    if status == 401:
        raise HTTPException(401, detail or "not authenticated", headers={"WWW-Authenticate": "Bearer"})
    raise HTTPException(403, detail or "forbidden")


# ---------- login with lockout ----------

async def check_password(password, password_hash):
    """bcrypt is slow on purpose (about 50 ms), so it runs in a thread and does not block other requests."""
    hashed = password_hash.encode() if password_hash else DUMMY_HASH
    ok = await asyncio.to_thread(bcrypt.checkpw, password.encode(), hashed)
    return ok and password_hash is not None


async def login(request, username, password):
    if not ON:
        raise HTTPException(404, "authentication is off in this mode")
    import common  # imported here: common imports this module
    user = await common.fetchrow(
        "SELECT id, password_hash, role, student_id, coalesce(locked_until > now(), false) AS locked "
        "FROM users WHERE username = $1", username)
    if user and user["locked"]:
        await deny(request, 401, "locked", action="login", detail="too many failed logins, try again later",
                   user_id=username)
    if not await check_password(password, user["password_hash"] if user else None):
        if user:
            # 5 failures in a row lock the account for 60 s; after the lock the count starts again.
            locked = await common.fetchval(
                "UPDATE users SET "
                "  failed_logins = CASE WHEN failed_logins + 1 >= $2 THEN 0 ELSE failed_logins + 1 END, "
                "  locked_until = CASE WHEN failed_logins + 1 >= $2 THEN now() + make_interval(secs => $3) "
                "                 ELSE locked_until END "
                "WHERE id = $1 RETURNING failed_logins = 0", user["id"], MAX_FAILED_LOGINS, float(LOCK_S))
            if locked:
                await audit(request, "lockout", username, f"locked for {LOCK_S} s", user_id=username)
        # Same answer for a wrong password and an unknown user: no hint which usernames exist.
        await deny(request, 401, "bad_password" if user else "unknown_user", action="login",
                   detail="invalid username or password", user_id=username)
    await common.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = $1", user["id"])
    obs.user_id.set(username)
    await audit(request, "login", username, "success")
    return {"access_token": create_token(username, user["role"], user["student_id"]),
            "token_type": "bearer", "expires_in": TOKEN_TTL_S}


# ---------- audit log ----------

def client_ip(request):
    return request.headers.get("x-real-ip") or (request.client.host if request.client else None)


async def audit(request, action, resource, result, user_id=None):
    """One row in audit_log (the app role may only INSERT there) plus one JSON log line."""
    if not ON:
        return
    import common
    user_id = user_id or obs.user_id.get()
    ip = client_ip(request)
    level = logging.WARNING if result.startswith(("denied", "failed")) else logging.INFO
    obs.log("audit", f"{action} {resource}: {result}", level=level, user_id=user_id, action=action,
            resource=resource, result=result, source_ip=ip)
    try:
        await common.execute(
            "INSERT INTO audit_log (request_id, user_id, action, resource, result, source_ip) "
            "VALUES ($1, $2, $3, $4, $5, $6)", obs.request_id.get(), user_id, action, resource, result, ip)
    except Exception as e:  # the DB may be down: the JSON line above is still a record
        obs.log("audit_db_failed", repr(e), level=logging.ERROR)


# ---------- bad input must give 422, never 500 ----------

def install(app):
    """Defense in depth behind the input models: values the DB refuses are client errors."""
    if not ON:
        return

    async def bad_value(request, exc):
        return JSONResponse({"detail": "invalid value"}, status_code=422)

    app.add_exception_handler(asyncpg.DataError, bad_value)
    app.add_exception_handler(asyncpg.IntegrityConstraintViolationError, bad_value)
