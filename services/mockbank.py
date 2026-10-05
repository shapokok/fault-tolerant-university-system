"""mock-bank: fake external payment provider with a fault-injection switch."""
import asyncio

from fastapi import HTTPException
from pydantic import BaseModel

import common

app = common.create_app("mock-bank", use_db=False)

MODES = {"none", "delay", "error", "crash_after_debit"}
fault = {"mode": "none", "delay_s": 0.0}
charges = []  # every debit the bank has made (kept in memory)


class Fault(BaseModel):
    mode: str = "none"
    delay_s: float = 0.0


class Charge(BaseModel):
    ref: str  # payment idempotency key, lets reconciliation look a charge up later
    amount: str


@app.post("/admin/fault")
async def set_fault(f: Fault):
    if f.mode not in MODES:
        raise HTTPException(400, f"mode must be one of {sorted(MODES)}")
    fault.update(mode=f.mode, delay_s=f.delay_s)
    return fault


@app.get("/admin/fault")
async def get_fault():
    return fault


@app.post("/charge")
async def charge(c: Charge):
    if fault["mode"] == "delay":
        await asyncio.sleep(fault["delay_s"])
    if fault["mode"] == "error":
        raise HTTPException(503, "bank unavailable")

    bank_ref = f"bank-{len(charges) + 1}"
    charges.append({"bank_ref": bank_ref, "ref": c.ref, "amount": c.amount})

    if fault["mode"] == "crash_after_debit":
        # Money is taken, but the caller never gets a confirmation.
        raise HTTPException(500, "crashed after debit")
    return {"bank_ref": bank_ref, "status": "debited"}


@app.get("/charges")
async def list_charges(ref: str | None = None):
    # Used by reconciliation (by ref) and by the consistency check (all).
    return [ch for ch in charges if ref is None or ch["ref"] == ref]
