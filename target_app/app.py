import os
import threading
import time

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Target Microservice", version="1.0.0")

UPSTREAM_URL = os.getenv("UPSTREAM_URL", "http://upstream:9000")
_inventory_lock = threading.Lock()


class OrderRequest(BaseModel):
    item_id: str
    quantity: int


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/v1/catalog")
async def get_catalog():
    return {"catalog": [{"id": "item-1", "name": "Widget Alpha", "price": 10.0}]}


@app.get("/api/v1/status")
async def get_status():
    return {"healthy": True, "active": True}


@app.post("/api/v1/order")
async def create_order(order: OrderRequest):
    if order.quantity < 0:
        raise ValueError("CRITICAL_ORDER_PANIC: Negative quantity rejected by core ledger!")
    if order.quantity == 0:
        raise ZeroDivisionError("CRITICAL_ORDER_PANIC: Division by zero quantity in allocation!")
    return {"status": "accepted", "item_id": order.item_id, "quantity": order.quantity}


@app.post("/api/v1/inventory/reserve")
async def reserve_inventory():
    with _inventory_lock:
        time.sleep(0.12)
        return {"reserved": True}


@app.get("/api/v1/payment/verify")
async def verify_payment(delay: float = 4):
    async with httpx.AsyncClient(timeout=None) as client:
        response = await client.get(f"{UPSTREAM_URL}/api/upstream/slow?delay={delay}")
        return {"verified": True, "upstream": response.json()}
