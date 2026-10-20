import os
import threading
import time

import httpx
from fastapi import FastAPI, Request
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
        __import__("logging").getLogger("target_app").error("Traceback (most recent call last): CRITICAL_ORDER_PANIC: Negative quantity rejected by core ledger!"); raise ValueError("CRITICAL_ORDER_PANIC: Negative quantity rejected by core ledger!")
    if order.quantity == 0:
        __import__("logging").getLogger("target_app").error("Traceback (most recent call last): CRITICAL_ORDER_PANIC: Division by zero quantity in allocation!"); raise ZeroDivisionError("CRITICAL_ORDER_PANIC: Division by zero quantity in allocation!")
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


# Deliberately unbounded response cache used by the LEAK chaos vector.  The
# small helper API is also the seam used by a bounded-cache repair strategy
# and by offline tests; the vulnerable default remains an ordinary dict.
_cache: dict[str, str] = {}
_unbounded_cache: list[dict] = []


def cache_size() -> int:
    """Return the number of entries retained by the target cache."""
    return len(_cache)


def clear_cache() -> None:
    """Clear retained cache entries between deterministic attack runs."""
    _cache.clear()


@app.get("/api/v1/cache")
async def cache_response(
    request: Request,
    key: str = "default",
    value: str | None = None,
    payload_size: int = 1024,
):
    if request.method == "POST":
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if isinstance(body, dict):
            key = str(body.get("key", key))
            value = body.get("value", value)
            if value is not None:
                value = str(value)
    if value is None:
        value = "x" * max(0, min(payload_size, 1_000_000))
    _cache[key] = value
    return {"cached": True, "key": key, "cache_size": cache_size()}


@app.post("/api/v1/cache")
async def cache_response_post(request: Request, key: str = "default", value: str | None = None, payload_size: int = 1024):
    return await cache_response(request, key=key, value=value, payload_size=payload_size)


@app.post("/api/v1/cache/item")
async def cache_item(payload: dict):
    """Deliberately retain every payload for the resource-exhaustion vector."""
    _unbounded_cache.append({"data": payload, "ts": time.time()})
    return {"cached": True, "count": len(_unbounded_cache)}
