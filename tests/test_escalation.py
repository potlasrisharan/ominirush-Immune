from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
from pathlib import Path

import httpx
import pytest

from omniimmune.arbiter import Arbiter
from omniimmune.chaos.fuzzer import FuzzNet
from omniimmune.chaos.latency import LatencyDriftAgent
from omniimmune.chaos.storm import StormLoad
from omniimmune.healer.patcher import PatchLadder
from omniimmune.memory import ImmuneMemory
from omniimmune.models import (
    AttackDNA,
    AttackType,
    BugClass,
    IncidentSignature,
    LatencyStats,
    VerificationVerdict,
)
from omniimmune.verifier.escalation import EscalationEngine


ROOT = Path(__file__).resolve().parents[1]
TARGET_SOURCE = ROOT / "target_app" / "app.py"


def load_app(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def patched_target(tmp_path: Path, bug_class: BugClass):
    target = tmp_path / f"target_{bug_class.value}.py"
    shutil.copyfile(TARGET_SOURCE, target)
    PatchLadder(target).apply_patch(bug_class, f"sig-{bug_class.value}")
    return load_app(target, f"patched_{bug_class.value}")


def test_mutations_are_deterministic_integrity_checked_and_endpoint_aware(tmp_path: Path) -> None:
    original = AttackDNA(
        attack_type=AttackType.FUZZ,
        target_endpoint="/api/v1/order",
        http_method="POST",
        payload={"item_id": "item-1", "quantity": 3},
        headers={"Content-Type": "application/json", "X-Test": "kept"},
        query_params={"unused": "kept"},
        seed=17,
    )
    engine = EscalationEngine(seed=99)
    first = engine.mutate_dna(original)
    second = EscalationEngine(seed=99).mutate_dna(original)

    assert len(first) == 3
    assert [dna.model_dump(mode="json") for dna in first] == [
        dna.model_dump(mode="json") for dna in second
    ]
    assert all(isinstance(dna, AttackDNA) and dna.verify_integrity() for dna in first)
    assert {dna.dna_id for dna in first}.__len__() == 3
    assert {dna.sha256 for dna in first}.__len__() == 3
    assert {dna.seed for dna in first} == {original.seed + 1000}
    assert all(
        (dna.target_endpoint, dna.http_method, dna.attack_type, dna.headers, dna.query_params)
        == (original.target_endpoint, original.http_method, original.attack_type, original.headers, original.query_params)
        for dna in first
    )
    assert first[0].payload["quantity"] == -9999999999
    assert first[1].payload["quantity"] == "🔥_null_byte_\x00_' OR 1=1 --"
    assert first[2].payload["quantity"] == {"$nested": {"items": [-1, "unexpected"]}}
    assert first[2].raw_body is not None
    with pytest.raises(ValueError):
        json.loads(first[2].raw_body)
    assert original.payload == {"item_id": "item-1", "quantity": 3}

    for index, dna in enumerate(first):
        path = tmp_path / f"mutation-{index}.json"
        dna.to_file(path)
        loaded = AttackDNA.from_file(path)
        assert loaded.dna_id == dna.dna_id
        assert loaded.sha256 == dna.sha256
        assert loaded.verify_integrity()


@pytest.mark.asyncio
async def test_mutations_for_bodyless_and_query_endpoints_change_relevant_workload() -> None:
    storm_original = StormLoad(default_concurrency=2).build_dna(concurrency=2, request_count=3)
    storm_mutations = EscalationEngine().mutate_dna(storm_original)
    assert len({(dna.concurrency, dna.request_count) for dna in storm_mutations}) == 3
    assert all(dna.payload is None and dna.target_endpoint == storm_original.target_endpoint for dna in storm_mutations)

    latency_original = LatencyDriftAgent().build_dna(delay_seconds=4.0)
    latency_mutations = EscalationEngine().mutate_dna(latency_original)
    assert [dna.query_params["delay"] for dna in latency_mutations] == [
        "0",
        "🔥_null_byte_\x00_' OR 1=1 --",
        '{"$nested":{"items":[-1,"unexpected"]}}',
    ]


@pytest.mark.asyncio
async def test_escalation_against_patched_order_runs_all_mutations_and_health(tmp_path: Path) -> None:
    app = patched_target(tmp_path, BugClass.PANIC)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    original = FuzzNet(seed=23).build_dna(0)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        result = await EscalationEngine(seed=7).run_escalation("http://test", original, client)

    assert result["all_survived"] is True
    assert result["mutations_tested"] == 3
    assert len(result["details"]) == 3
    assert all(detail["health_ok"] and detail["survived"] for detail in result["details"])
    assert result["details"][0]["status_code"] == 400
    assert result["details"][1]["status_code"] == 422
    assert result["details"][2]["status_code"] == 422
    assert result["details"][0]["supplementary"]["status_code"] == 200


@pytest.mark.asyncio
async def test_escalation_accepts_controlled_timeout_and_rejects_server_errors() -> None:
    calls: list[str] = []

    async def healthy_timeout(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(504, json={"error": "upstream_timeout_handled"})

    latency = LatencyDriftAgent().build_dna(4.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(healthy_timeout), base_url="http://test") as client:
        accepted = await EscalationEngine().run_escalation("http://test", latency, client)
    assert accepted["all_survived"] is True
    assert calls.count("/health") == 3

    async def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken), base_url="http://test") as client:
        rejected = await EscalationEngine().run_escalation("http://test", latency, client)
    assert rejected["all_survived"] is False
    assert rejected["mutations_tested"] == 3
    assert any(not detail["survived"] for detail in rejected["details"])


@pytest.mark.asyncio
async def test_escalation_records_timeout_health_failure_and_partial_execution(monkeypatch) -> None:
    original = FuzzNet().build_dna(0)
    engine = EscalationEngine()
    replay_calls = 0

    async def stop_after_one(*args, **kwargs):
        nonlocal replay_calls
        replay_calls += 1
        if replay_calls == 1:
            return {"status_code": 200, "elapsed_ms": 1.0, "error": None}
        raise TimeoutError("late mutation")

    monkeypatch.setattr("omniimmune.verifier.escalation.replay", stop_after_one)

    async def health_failure(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(health_failure), base_url="http://test") as client:
        result = await engine.run_escalation("http://test", original, client)

    assert result["all_survived"] is False
    assert result["mutations_tested"] == 3
    assert all(detail["health_ok"] is False for detail in result["details"])


def test_verification_verdict_is_not_escalated_by_default() -> None:
    verdict = VerificationVerdict(
        sig_id="sig-test",
        dna_id="dna-test",
        passed=True,
        after=LatencyStats(
            p50_ms=1,
            p95_ms=1,
            p99_ms=1,
            error_rate=0,
            survival_rate=1,
            peak_mem_mb=0,
        ),
        regression_passed=True,
    )
    assert verdict.escalation_passed is None
    assert verdict.mutations_tested == 0


def _baseline_verdict(passed: bool) -> VerificationVerdict:
    return VerificationVerdict(
        sig_id="sig-memory",
        dna_id="dna-memory",
        passed=passed,
        after=LatencyStats(
            p50_ms=1,
            p95_ms=1,
            p99_ms=1,
            error_rate=0,
            survival_rate=1,
            peak_mem_mb=0,
        ),
        regression_passed=passed,
    )


@pytest.mark.asyncio
async def test_cached_patch_runs_escalation_only_after_baseline_passes(tmp_path: Path, monkeypatch) -> None:
    arbiter = Arbiter("http://test", "http://upstream", mode="local", no_ui=True)
    arbiter.memory = ImmuneMemory(tmp_path / "memory")
    signature = IncidentSignature(
        sig_id="sig-memory",
        bug_class=BugClass.PANIC,
        failure_kind="exception",
        endpoint="/api/v1/order",
    )
    dna = FuzzNet().build_dna(0)
    arbiter.memory.save_patch(signature.sig_id, {"strategy": "cached", "diff": "", "verified": True})
    monkeypatch.setattr(arbiter, "_restart_target", lambda: asyncio.sleep(0))

    class FakeVerifier:
        async def verify_attack_dna(self, *args, **kwargs):
            return _baseline_verdict(True)

    class FakeEscalation:
        calls = 0

        async def run_escalation(self, *args, **kwargs):
            self.calls += 1
            return {
                "all_survived": True,
                "mutations_tested": 3,
                "details": [{"mutation": "numeric_boundary", "survived": True}],
            }

    escalation = FakeEscalation()
    arbiter.escalation = escalation
    patch, verdict = await arbiter._patch_and_verify(
        signature,
        dna,
        FakeVerifier(),
        object(),
        PatchLadder(tmp_path / "unused.py"),
        "",
    )
    assert patch.from_memory is True
    assert escalation.calls == 1
    assert verdict.passed is True
    assert verdict.escalation_passed is True
    assert verdict.mutations_tested == 3
    assert [event.type for event in arbiter.event_bus.history] == [
        "memory_hit",
        "escalation_started",
        "escalation_passed",
    ]


@pytest.mark.asyncio
async def test_baseline_failure_prevents_escalation_and_certification(tmp_path: Path, monkeypatch) -> None:
    arbiter = Arbiter("http://test", "http://upstream", mode="local", no_ui=True)
    arbiter.memory = ImmuneMemory(tmp_path / "memory")
    signature = IncidentSignature(
        sig_id="sig-memory",
        bug_class=BugClass.PANIC,
        failure_kind="exception",
        endpoint="/api/v1/order",
    )
    dna = FuzzNet().build_dna(0)
    arbiter.memory.save_patch(signature.sig_id, {"strategy": "cached", "diff": "", "verified": True})
    monkeypatch.setattr(arbiter, "_restart_target", lambda: asyncio.sleep(0))

    class FakeVerifier:
        async def verify_attack_dna(self, *args, **kwargs):
            return _baseline_verdict(False)

    class FakeEscalation:
        calls = 0

        async def run_escalation(self, *args, **kwargs):
            self.calls += 1
            return {"all_survived": True, "mutations_tested": 3, "details": []}

    escalation = FakeEscalation()
    arbiter.escalation = escalation
    _, verdict = await arbiter._patch_and_verify(
        signature,
        dna,
        FakeVerifier(),
        object(),
        PatchLadder(tmp_path / "unused.py"),
        "",
    )
    assert verdict.passed is False
    assert verdict.escalation_passed is None
    assert escalation.calls == 0
    assert not any(event.type == "escalation_passed" for event in arbiter.event_bus.history)
