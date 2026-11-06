"""Shared code for all services: config, DB access with replica fallback, /health and /ready."""
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

import ft
import obs
import security

FT_MODE = os.getenv("FT_MODE", "off") == "on"
INSTANCE = obs.INSTANCE

# Primary first. The replica is only set in the ft compose file.
DB_URLS = {"primary": os.getenv("DATABASE_URL", "postgresql://uni:uni@postgres-primary:5432/uni")}
if os.getenv("REPLICA_URL"):
    DB_URLS["replica"] = os.environ["REPLICA_URL"]

# Errors that mean "this database server is unreachable", so try the next one.
# (TimeoutError is a subclass of OSError, so a DB timeout counts too.)
CONN_ERRORS = (
    OSError,
    asyncpg.InterfaceError,
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.AdminShutdownError,
)

pools = {}


def http_client(auth=None):
    # FT: explicit timeout on every outgoing call. Baseline: httpx default (5 s).
    # Both: forward the correlation ID.
    # auth: an Authorization header to pass on (only to our own services, never to the bank).
    headers = obs.outgoing_headers()
    if auth:
        headers["Authorization"] = auth
    return httpx.AsyncClient(timeout=ft.HTTP_TIMEOUT_S, headers=headers) if FT_MODE else httpx.AsyncClient(headers=headers)


async def get_pool(url):
    if url not in pools:
        if FT_MODE:
            pools[url] = await asyncpg.create_pool(
                url, min_size=0, max_size=10, timeout=ft.DB_TIMEOUT_S, command_timeout=ft.DB_TIMEOUT_S)
        else:
            pools[url] = await asyncpg.create_pool(url, min_size=0, max_size=10)
    return pools[url]


async def _db_once(method, query, *args):
    """Run a query on the primary. If the primary is unreachable, use the replica.

    The replica is read-only until infra/failover.sh promotes it, so writes there
    raise ReadOnlySQLTransactionError, which create_app turns into HTTP 503.
    """
    last_error = None
    for url in DB_URLS.values():
        try:
            pool = await get_pool(url)
            return await getattr(pool, method)(query, *args)
        except CONN_ERRORS as e:
            last_error = e
    raise last_error


async def db(method, query, *args):
    if FT_MODE:
        return await ft.retry(lambda: _db_once(method, query, *args), lambda e: isinstance(e, CONN_ERRORS))
    return await _db_once(method, query, *args)


async def fetch(query, *args):
    return await db("fetch", query, *args)


async def fetchrow(query, *args):
    return await db("fetchrow", query, *args)


async def fetchval(query, *args):
    return await db("fetchval", query, *args)


async def execute(query, *args):
    return await db("execute", query, *args)


@asynccontextmanager
async def transaction():
    """A DB connection inside a transaction: commit at the end, rollback on any error."""
    last_error = None
    for url in DB_URLS.values():
        try:
            pool = await get_pool(url)
            conn = await pool.acquire()
            break
        except CONN_ERRORS as e:
            last_error = e
    else:
        raise last_error
    try:
        async with conn.transaction():
            yield conn
    finally:
        await pool.release(conn)


def create_app(name, use_db=True, deps=None, breakers=()):
    """deps: {name: url} checked by /ready in FT mode. breakers: CircuitBreakers shown in /ready."""
    obs.setup(name)
    for b in breakers:
        obs.set_breaker_state(b.name, b.state)  # so the gauge exists (0) before the first failure
    app = FastAPI(title=name)

    @app.middleware("http")
    async def observe(request, call_next):
        rid = obs.read_request_id(request.headers.get("x-request-id"))
        obs.request_id.set(rid)
        box = {"retries": 0}
        ft.retry_box.set(box)
        start = obs.now()
        try:
            response = await call_next(request)
        except Exception:
            # Unhandled error: still count it, log it and return the IDs, the status stays 500.
            obs.log("unhandled_error", f"{request.method} {request.url.path}", level=logging.ERROR, exc_info=True)
            response = JSONResponse({"detail": "internal server error"}, status_code=500)
        route = request.scope.get("route")  # the template (/students/{sid}), so labels stay few
        source = "probe" if request.headers.get("x-probe") == "1" else "user"
        if response.headers.get("x-replayed") == "1":
            source = "replay"  # duplicate payment answered with the stored result
        obs.record_request(request.method, request.url.path, route.path if route else "unmatched",
                           response.status_code, obs.now() - start, source)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Attempts"] = str(1 + box["retries"])
        response.headers["X-Instance"] = INSTANCE
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.exception_handler(asyncpg.ReadOnlySQLTransactionError)
    async def read_only(request, exc):
        return JSONResponse({"detail": "database is read-only (primary down)"}, status_code=503)

    async def db_down(request, exc):
        return JSONResponse({"detail": f"database unavailable: {type(exc).__name__}"}, status_code=503)

    for error in CONN_ERRORS:
        app.add_exception_handler(error, db_down)
    security.install(app)

    @app.get("/health")
    async def health():
        # Liveness: the process is up and answering.
        return {"status": "ok", "service": name, "instance": INSTANCE, "ft_mode": FT_MODE}

    @app.get("/ready")
    async def ready():
        # Readiness: database, and in FT mode also dependencies and circuit breakers.
        body = {"service": name, "db": None, "deps": {}, "breakers": {b.name: b.state for b in breakers}}
        problems = []
        if use_db:
            for db_name, url in DB_URLS.items():
                try:
                    await (await get_pool(url)).fetchval("SELECT 1")
                    body["db"] = db_name
                    break
                except CONN_ERRORS:
                    pass
            if body["db"] is None:
                problems.append("db down")
        if FT_MODE:
            # Timeout below the monitor timeout (0.4 s).
            async with httpx.AsyncClient(timeout=0.3, headers={**obs.outgoing_headers(), "X-Probe": "1"}) as client:
                for dep, url in (deps or {}).items():
                    try:
                        ok = (await client.get(url)).status_code < 500
                    except httpx.HTTPError:
                        ok = False
                    body["deps"][dep] = "up" if ok else "down"
                    if not ok:
                        problems.append(f"{dep} down")
            problems += [f"breaker {b.name} {b.state}" for b in breakers if b.state != "closed"]
        body["status"] = "not ready" if problems else "ready"
        body["reason"] = ", ".join(problems)
        return JSONResponse(body, status_code=503 if problems else 200)

    return app
