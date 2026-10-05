"""payment-service: tuition payments, charges the card through mock-bank.

Baseline: insert, charge, update. No idempotency, no transaction, no retry.
FT: idempotency key, retry only when the bank surely did not debit, circuit breaker,
completion in one transaction, reconciliation of payments with an unknown outcome.
"""
import asyncio
import os
import uuid
from decimal import Decimal

import asyncpg
import httpx
from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import common
import ft

BANK_URL = os.getenv("BANK_URL", "http://mock-bank:8000")
RECONCILE_EVERY_S = 5
RECONCILE_MIN_AGE_S = 30  # only touch payments older than this (longer than any bank delay)

bank_breaker = ft.CircuitBreaker("mock-bank")
app = common.create_app("payment-service", deps={"mock-bank": f"{BANK_URL}/health"}, breakers=[bank_breaker])


class PaymentIn(BaseModel):
    student_id: int
    amount: Decimal


async def charge(key, amount):
    async with common.http_client() as client:
        r = await client.post(f"{BANK_URL}/charge", json={"ref": key, "amount": str(amount)})
    r.raise_for_status()
    return r.json()


def bank_did_not_debit(e):
    """True only when we are sure no money moved, so retrying cannot charge twice.
    A read timeout or a 500 means "unknown": the bank may have debited."""
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 503


async def complete(pid, bank_ref, amount):
    """Mark completed and write the ledger in ONE transaction: both happen or neither (rollback)."""
    async with common.transaction() as conn:
        done = await conn.fetchval(
            "UPDATE payments SET status = 'completed', bank_ref = $2 "
            "WHERE id = $1 AND status = 'pending' RETURNING id", pid, bank_ref)
        if done:  # guard: only one caller (request or reconciliation) may write the ledger
            await conn.execute("INSERT INTO ledger (payment_id, amount) VALUES ($1, $2)", pid, amount)
        return done is not None


def replay(row):
    """Duplicate request: return the result of the original payment, do nothing new."""
    body = {"payment_id": row["id"], "status": row["status"], "bank_ref": row["bank_ref"], "replayed": True}
    code = {"completed": 200, "pending": 202}.get(row["status"], 502)
    return JSONResponse(body, status_code=code)


@app.post("/payments", status_code=201)
async def create_payment(p: PaymentIn, idempotency_key: str | None = Header(None)):
    key = idempotency_key or str(uuid.uuid4())
    if not common.FT_MODE:
        return await baseline_payment(p, key)

    # 1. Duplicate request detection (UNIQUE index on idempotency_key).
    existing = await common.fetchrow("SELECT * FROM payments WHERE idempotency_key = $1", key)
    if existing:
        return replay(existing)
    pid = await common.fetchval(
        "INSERT INTO payments (student_id, amount, status, idempotency_key) VALUES ($1, $2, 'pending', $3) "
        "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
        p.student_id, p.amount, key,
    )
    if pid is None:  # the same key arrived at the same moment on another request
        return replay(await common.fetchrow("SELECT * FROM payments WHERE idempotency_key = $1", key))

    # 2. Charge: circuit breaker around retry with backoff.
    try:
        result = await bank_breaker.call(lambda: ft.retry(lambda: charge(key, p.amount), bank_did_not_debit))
    except ft.CircuitOpen:
        await common.execute("UPDATE payments SET status = 'failed' WHERE id = $1", pid)
        raise HTTPException(503, "bank unavailable (circuit open), try again later")
    except httpx.HTTPError as e:
        if bank_did_not_debit(e):
            await common.execute("UPDATE payments SET status = 'failed' WHERE id = $1", pid)
            raise HTTPException(502, f"bank error: {type(e).__name__}")
        # Unknown outcome (timeout, bank crashed after debit): keep it pending,
        # reconciliation asks the bank later and completes or fails it.
        return JSONResponse({"payment_id": pid, "status": "pending"}, status_code=202)

    # 3. Completion in one transaction. If the DB fails here, the payment stays
    # pending and reconciliation completes it (the bank has the charge).
    await complete(pid, result["bank_ref"], p.amount)
    return {"payment_id": pid, "status": "completed", "bank_ref": result["bank_ref"]}


async def baseline_payment(p, key):
    pid = await common.fetchval(
        "INSERT INTO payments (student_id, amount, status, idempotency_key) "
        "VALUES ($1, $2, 'pending', $3) RETURNING id",
        p.student_id, p.amount, key,
    )
    try:
        result = await charge(key, p.amount)
    except httpx.HTTPError as e:
        await common.execute("UPDATE payments SET status = 'failed' WHERE id = $1", pid)
        raise HTTPException(502, f"bank error: {e}")
    await common.execute("UPDATE payments SET status = 'completed', bank_ref = $2 WHERE id = $1", pid, result["bank_ref"])
    await common.execute("INSERT INTO ledger (payment_id, amount) VALUES ($1, $2)", pid, p.amount)
    return {"payment_id": pid, "status": "completed", "bank_ref": result["bank_ref"]}


@app.get("/payments/{pid}")
async def get_payment(pid: int):
    row = await common.fetchrow("SELECT * FROM payments WHERE id = $1", pid)
    if not row:
        raise HTTPException(404, "payment not found")
    return dict(row)


# ---------- reconciliation (FT only) ----------

async def reconcile(min_age_s=RECONCILE_MIN_AGE_S):
    """For every old pending payment ask the bank: charge found -> completed, else failed."""
    rows = await common.fetch(
        "SELECT id, idempotency_key, amount FROM payments "
        "WHERE status = 'pending' AND created_at < now() - make_interval(secs => $1)", float(min_age_s))
    fixed = {"completed": 0, "failed": 0}
    for row in rows:
        async with common.http_client() as client:
            r = await client.get(f"{BANK_URL}/charges", params={"ref": row["idempotency_key"]})
        r.raise_for_status()
        charges = r.json()
        if charges:
            fixed["completed"] += await complete(row["id"], charges[0]["bank_ref"], row["amount"])
        else:
            status = await common.execute(
                "UPDATE payments SET status = 'failed' WHERE id = $1 AND status = 'pending'", row["id"])
            fixed["failed"] += status == "UPDATE 1"
    return fixed


async def reconcile_loop():
    while True:
        await asyncio.sleep(RECONCILE_EVERY_S)
        try:
            fixed = await reconcile()
            if any(fixed.values()):
                print(f"reconciliation: {fixed}", flush=True)
        except Exception as e:
            print(f"reconciliation failed, will retry: {e!r}", flush=True)


@app.post("/admin/reconcile")
async def reconcile_now(min_age_s: float = 0):
    if not common.FT_MODE:
        raise HTTPException(404, "no reconciliation in baseline")
    return await reconcile(min_age_s)


@app.on_event("startup")
async def ft_startup():
    if not common.FT_MODE:
        return
    try:  # UNIQUE constraint behind idempotency (baseline does not have it)
        await common.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS payments_idempotency_key ON payments (idempotency_key)")
    except asyncpg.PostgresError:
        pass  # the other instance created it at the same moment
    asyncio.create_task(reconcile_loop())
