import ast
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from target_app import app as target_module


def test_golden_endpoints_return_exact_json():
    with TestClient(target_module.app) as client:
        assert client.get("/health").json() == {"status": "ok", "version": "1.0.0"}
        assert client.get("/api/v1/catalog").json() == {
            "catalog": [{"id": "item-1", "name": "Widget Alpha", "price": 10.0}]
        }
        assert client.get("/api/v1/status").json() == {"healthy": True, "active": True}


def test_order_bug_shapes_and_happy_path():
    with TestClient(target_module.app) as client:
        with pytest.raises(ValueError, match="CRITICAL_ORDER_PANIC: Negative quantity"):
            client.post("/api/v1/order", json={"item_id": "item-1", "quantity": -1})
        with pytest.raises(ZeroDivisionError, match="CRITICAL_ORDER_PANIC: Division by zero"):
            client.post("/api/v1/order", json={"item_id": "item-1", "quantity": 0})
        response = client.post("/api/v1/order", json={"item_id": "item-1", "quantity": 3})
        assert response.status_code == 200
        assert response.json() == {"status": "accepted", "item_id": "item-1", "quantity": 3}

    client = TestClient(target_module.app, raise_server_exceptions=False)
    assert client.post("/api/v1/order", json={"item_id": "item-1", "quantity": -1}).status_code == 500
    assert client.post("/api/v1/order", json={"item_id": "item-1", "quantity": 0}).status_code == 500
    client.close()


@pytest.mark.asyncio
async def test_reserve_serializes_concurrent_requests():
    transport = httpx.ASGITransport(app=target_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = time.perf_counter()
        responses = await asyncio.gather(
            *(client.post("/api/v1/inventory/reserve") for _ in range(5))
        )
        elapsed = time.perf_counter() - started
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json() == {"reserved": True} for response in responses)
    assert elapsed >= 0.50


@pytest.mark.asyncio
async def test_payment_verify_uses_uncapped_client_and_delay(monkeypatch):
    calls = {}

    class FakeResponse:
        def json(self):
            return {"status": "upstream_ok"}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            calls["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            calls["url"] = url
            return FakeResponse()

    monkeypatch.setattr(target_module, "httpx", SimpleNamespace(AsyncClient=FakeAsyncClient))
    transport = httpx.ASGITransport(app=target_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/payment/verify")
    assert response.status_code == 200
    assert calls == {
        "timeout": None,
        "url": f"{target_module.UPSTREAM_URL}/api/upstream/slow?delay=4.0",
    }


def test_bug_shapes_are_present():
    source = Path(target_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    has_lock_assignment = any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_inventory_lock" for target in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "threading"
        and node.value.func.attr == "Lock"
        for node in tree.body
    )
    assert has_lock_assignment

    reserve_functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "reserve_inventory"
    ]
    assert reserve_functions
    reserve_source = ast.unparse(reserve_functions[0])
    assert "with _inventory_lock" in reserve_source
    assert "time.sleep(0.12)" in reserve_source

    has_uncapped_client = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
        and node.func.attr == "AsyncClient"
        and any(
            keyword.arg == "timeout"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is None
            for keyword in node.keywords
        )
        for node in ast.walk(tree)
    )
    assert has_uncapped_client
