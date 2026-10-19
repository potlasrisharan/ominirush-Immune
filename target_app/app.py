import os
import time
import threading
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Target Microservice", version="1.0.0")

_inventory_lock = threading.Lock()
UPSTREAM_URL = os.getenv("OMNI_UPSTREAM_URL", "http://upstream:8001")


# Golden Happy-Path Endpoints (Used for regression verification)
@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/v1/catalog")
async def get_catalog():
    return {
        "catalog": [
            {"id": "item-1", "name": "Widget Alpha", "price": 10.0},
            {"id": "item-2", "name": "Widget Beta", "price": 25.0},
        ]
    }


@app.get("/api/v1/status")
async def get_status():
    return {"healthy": True, "active": True}


# BUG 1: Unhandled route panic on negative/malformed JSON values
# Real traceback emitted in stderr
@app.post("/api/v1/order")
async def create_order(payload: dict):
    item_id = payload.get("item_id", "default_item")
    quantity = payload.get("quantity", 1)
    # BUG_1_START: Unhandled route panic on negative values
    if quantity < 0:
        raise ValueError(f"CRITICAL_ORDER_PANIC: Negative quantity {quantity} is rejected by core ledger!")
    if quantity == 0:
        raise ZeroDivisionError("CRITICAL_ORDER_PANIC: Division by zero quantity in allocation!")
    total_cost = 100.0 / quantity
    # BUG_1_END
    return {
        "status": "created",
        "item_id": item_id,
        "quantity": quantity,
        "total": total_cost,
    }


# BUG 2: Concurrency deadlock
# Blocking threading.Lock held across blocking sleep inside async def
@app.post("/api/v1/inventory/reserve")
async def reserve_inventory(payload: dict):
    sku = payload.get("sku", "SKU-DEFAULT")
    # BUG_2_START: Blocking threading.Lock held inside async def freezes event loop
    with _inventory_lock:
        time.sleep(0.12)
        return {"status": "reserved", "sku": sku}
    # BUG_2_END


# BUG 3: Uncapped upstream call
# httpx call with timeout=None against slow upstream, hanging container and breaching SLA
@app.get("/api/v1/payment/verify")
async def verify_payment(transaction_id: str = "tx_999"):
    # BUG_3_START: Uncapped upstream call with timeout=None
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.get(f"{UPSTREAM_URL}/api/upstream/slow?delay=4")
        return {"transaction_id": transaction_id, "upstream_response": resp.json()}
    # BUG_3_END
