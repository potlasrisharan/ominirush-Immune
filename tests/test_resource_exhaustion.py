from __future__ import annotations

import httpx
import pytest

from omniimmune.chaos.common import ATTACK_BUG_HINT
from omniimmune.chaos.memory import MemoryStressAgent
from omniimmune.healer.triage import TriageEngine
from omniimmune.models import AttackType, BugClass, IncidentSignature
from target_app import app as target_module


@pytest.mark.asyncio
async def test_cache_endpoint_retains_unique_entries_until_cleared() -> None:
    target_module.clear_cache()
    transport = httpx.ASGITransport(app=target_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/api/v1/cache", params={"key": "one", "payload_size": 8})
        second = await client.post("/api/v1/cache", json={"key": "two", "value": "retained"})
    assert first.status_code == second.status_code == 200
    assert first.json()["cache_size"] == 1
    assert second.json()["cache_size"] == 2
    assert target_module.cache_size() == 2
    target_module.clear_cache()
    assert target_module.cache_size() == 0


@pytest.mark.asyncio
async def test_memory_stress_agent_detects_cache_growth_and_reports_stats() -> None:
    target_module.clear_cache()
    agent = MemoryStressAgent(default_request_count=5, default_payload_size=32, concurrency=2)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=target_module.app), base_url="http://test"
    ) as client:
        dna, result = await agent.run("http://test", client=client)
    assert dna.attack_type is AttackType.LEAK
    assert dna.target_endpoint == "/api/v1/cache"
    assert result["is_failure"] is True
    assert result["failure_kind"] == "resource_exhaustion"
    assert result["cache_growth"] == 5
    assert result["stats"].peak_mem_mb > 0


def test_resource_exhaustion_model_and_triage_signature() -> None:
    assert ATTACK_BUG_HINT[AttackType.LEAK] is BugClass.RESOURCE_EXHAUSTION
    signature = TriageEngine().diagnose_from_resource_exhaustion(
        "/api/v1/cache", peak_mem_mb=2.5, dna_id="leak-dna"
    )
    assert signature.bug_class is BugClass.RESOURCE_EXHAUSTION
    assert signature.failure_kind == "resource_exhaustion"
    assert signature.culprit_function == "cache_response"
    assert signature.culprit_file == "target_app/app.py"
    assert signature.dna_id == "leak-dna"
    assert signature.sig_id == IncidentSignature.make_sig_id(
        BugClass.RESOURCE_EXHAUSTION, "/api/v1/cache", None
    )
