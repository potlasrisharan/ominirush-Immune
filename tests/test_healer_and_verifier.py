from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import httpx
import pytest

from omniimmune.chaos.fuzzer import FuzzNet
from omniimmune.healer.patcher import PatchLadder
from omniimmune.healer.triage import TriageEngine
from omniimmune.models import AttackType, BugClass, IncidentSignature
from omniimmune.chaos.latency import LatencyDriftAgent
from omniimmune.verifier.verifier import VerifierEngine


ROOT = Path(__file__).resolve().parents[1]
TARGET_SOURCE = ROOT / "target_app" / "app.py"


def load_app(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def copy_target(tmp_path: Path) -> Path:
    destination = tmp_path / "target_app.py"
    shutil.copyfile(TARGET_SOURCE, destination)
    return destination


def test_triage_exception_traceback_extracts_location_and_signature() -> None:
    stderr = """Traceback (most recent call last):
  File \"/workspace/target_app/app.py\", line 38, in create_order
    raise ValueError(\"CRITICAL_ORDER_PANIC\")
ValueError: CRITICAL_ORDER_PANIC: Negative quantity
"""
    signature = TriageEngine().diagnose_from_exception(
        stderr, "/api/v1/order", dna_id="dna-123"
    )
    assert signature.bug_class is BugClass.PANIC
    assert signature.exception_type == "ValueError"
    assert signature.culprit_file == "target_app/app.py"
    assert signature.culprit_line == 38
    assert signature.culprit_function == "create_order"
    assert signature.dna_id == "dna-123"
    assert signature.sig_id == IncidentSignature.make_sig_id(
        BugClass.PANIC, "/api/v1/order", "ValueError"
    )


def test_triage_traceback_handles_module_frame_without_exposing_host_path() -> None:
    stderr = """Traceback (most recent call last):
  File "/Users/private-user/checkout/worker.py", line 9, in <module>
    raise CustomFailure("boom")
CustomFailure: boom
"""
    signature = TriageEngine().diagnose_from_exception(stderr, "/api/v1/order")
    assert signature.exception_type == "CustomFailure"
    assert signature.culprit_file == "worker.py"
    assert signature.culprit_function == "<module>"
    assert "/Users/private-user" not in signature.culprit_file


def test_triage_ast_detects_lock_and_uncapped_client() -> None:
    engine = TriageEngine(TARGET_SOURCE)
    starvation = engine.diagnose_from_sla_breach(
        "/api/v1/inventory/reserve", 2500.0, dna_id="storm-dna"
    )
    timeout = engine.diagnose_from_timeout(
        "/api/v1/payment/verify", dna_id="latency-dna"
    )
    assert starvation.bug_class is BugClass.STARVATION
    assert starvation.culprit_function == "reserve_inventory"
    assert starvation.culprit_line == 45
    assert timeout.bug_class is BugClass.UPSTREAM_TIMEOUT
    assert timeout.culprit_function == "verify_payment"
    assert timeout.culprit_line == 52


def test_patch_ladder_panic_guard_compiles_and_generates_diff(tmp_path: Path) -> None:
    target = copy_target(tmp_path)
    pristine = target.read_text(encoding="utf-8")
    result = PatchLadder(target).apply_patch(BugClass.PANIC, "sig-panic")
    modified = target.read_text(encoding="utf-8")
    compile(modified, str(target), "exec")
    assert result.applied is True
    assert result.strategy == "input_guard"
    assert "quantity <= 0" in modified
    assert "JSONResponse" in modified
    assert result.diff.startswith("--- a/")
    assert "+++ b/" in result.diff
    PatchLadder(target).rollback(pristine)
    assert target.read_text(encoding="utf-8") == pristine


def test_patch_ladder_preserves_module_docstring(tmp_path: Path) -> None:
    target = copy_target(tmp_path)
    target.write_text('"""Target service metadata."""\n\n' + target.read_text(encoding="utf-8"), encoding="utf-8")
    PatchLadder(target).apply_patch(BugClass.PANIC, "sig-docstring")
    module = compile(target.read_text(encoding="utf-8"), str(target), "exec")
    namespace: dict[str, object] = {}
    exec(module, namespace)
    assert namespace["__doc__"] == "Target service metadata."


def test_patch_ladder_concurrency_uses_async_lock_and_nonblocking_sleep(tmp_path: Path) -> None:
    target = copy_target(tmp_path)
    result = PatchLadder(target).apply_patch(BugClass.STARVATION, "sig-starvation")
    modified = target.read_text(encoding="utf-8")
    compile(modified, str(target), "exec")
    assert result.strategy == "asyncio_lock_nonblocking"
    assert "_inventory_lock = asyncio.Lock()" in modified
    assert "async with _inventory_lock:" in modified
    assert "await asyncio.sleep(0.01)" in modified
    assert "time.sleep(0.12)" not in modified


def test_patch_ladder_thread_pool_strategy_offloads_sleep(tmp_path: Path) -> None:
    target = copy_target(tmp_path)
    result = PatchLadder(target).apply_patch(BugClass.STARVATION, "sig-starvation", 1)
    modified = target.read_text(encoding="utf-8")
    compile(modified, str(target), "exec")
    assert result.strategy == "thread_pool_offload"
    assert "await asyncio.to_thread(time.sleep, 0.01)" in modified


def test_patch_ladder_timeout_adds_bound_and_handled_exception(tmp_path: Path) -> None:
    target = copy_target(tmp_path)
    result = PatchLadder(target).apply_patch(BugClass.UPSTREAM_TIMEOUT, "sig-timeout")
    modified = target.read_text(encoding="utf-8")
    compile(modified, str(target), "exec")
    assert result.strategy == "explicit_timeout_guard"
    assert "timeout=1.5" in modified
    assert "httpx.TimeoutException" in modified
    assert "status_code=504" in modified


@pytest.mark.asyncio
async def test_verifier_replays_patched_app_and_checks_golden_paths(tmp_path: Path) -> None:
    target = copy_target(tmp_path)
    PatchLadder(target).apply_patch(BugClass.PANIC, "sig-panic")
    app = load_app(target, "patched_target_for_verifier")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    dna = FuzzNet().build_dna(0)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        verdict = await VerifierEngine("http://test").verify_attack_dna(
            dna,
            sig_id="sig-panic",
            strategy_used="input_guard",
            client=client,
        )
    assert dna.attack_type is AttackType.FUZZ
    assert verdict.passed is True
    assert verdict.regression_passed is True
    assert verdict.status_codes == {"400": 1}
    assert verdict.after.survival_rate == 1.0


@pytest.mark.asyncio
async def test_verifier_golden_regression_rejects_broken_health() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(500)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        assert await VerifierEngine("http://test").check_golden_regression(client) is False


@pytest.mark.asyncio
async def test_verifier_golden_regression_requires_object_payloads_and_boolean_health() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json=[{"status": "ok"}])
        if request.url.path == "/api/v1/catalog":
            return httpx.Response(200, json={"catalog": []})
        return httpx.Response(200, json={"healthy": "false"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        assert await VerifierEngine("http://test").check_golden_regression(client) is False


@pytest.mark.asyncio
async def test_verifier_rejects_tampered_attack_dna_before_replay() -> None:
    dna = FuzzNet().build_dna(0).model_copy(update={"payload": {"quantity": 999}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        with pytest.raises(ValueError, match="integrity"):
            await VerifierEngine("http://test").verify_attack_dna(
                dna, sig_id="sig-test", strategy_used="test", client=client
            )


@pytest.mark.asyncio
async def test_verifier_accepts_handled_validation_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/catalog":
            return httpx.Response(200, json={"catalog": []})
        if request.url.path == "/api/v1/status":
            return httpx.Response(200, json={"healthy": True})
        return httpx.Response(422, json={"detail": "invalid delay"})

    dna = LatencyDriftAgent().build_dna(4.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        verdict = await VerifierEngine("http://test").verify_attack_dna(
            dna, sig_id="sig-timeout", strategy_used="explicit_timeout_guard", client=client
        )
    assert verdict.passed is True
    assert verdict.status_codes == {"422": 1}
