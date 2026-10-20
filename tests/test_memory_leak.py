from __future__ import annotations

import httpx
import pytest

from omniimmune.chaos.memory import MemoryStressAgent
from omniimmune.healer.patcher import PatchLadder
from omniimmune.models import BugClass
from target_app import app as target_module


@pytest.mark.asyncio
async def test_memory_vector_uses_cache_item_and_detects_growth() -> None:
    target_module._unbounded_cache.clear()
    agent = MemoryStressAgent(default_request_count=4, default_payload_size=16, concurrency=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=target_module.app), base_url="http://test"
    ) as client:
        dna, result = await agent.run("http://test", client=client)

    assert dna.target_endpoint == "/api/v1/cache/item"
    assert dna.http_method == "POST"
    assert result["failure_kind"] == "resource_exhaustion"
    assert result["cache_growth"] == 4
    target_module._unbounded_cache.clear()


def test_bounded_cache_transformer_repairs_list_cache() -> None:
    source = """\
from fastapi import FastAPI
app = FastAPI()
_unbounded_cache: list[dict] = []

async def cache_item(payload: dict):
    _unbounded_cache.append(payload)
    return {"count": len(_unbounded_cache)}
"""
    target = target_module.__file__
    path = __import__("pathlib").Path(target)
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text(source, encoding="utf-8")
        result = PatchLadder(path).apply_patch(BugClass.RESOURCE_EXHAUSTION, "sig-leak")
        patched = path.read_text(encoding="utf-8")
        assert result.strategy == "bounded_cache"
        assert "deque(" in patched and "maxlen = 1000" in patched
        compile(patched, str(path), "exec")
    finally:
        path.write_text(original, encoding="utf-8")
