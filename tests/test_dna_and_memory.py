from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from omniimmune.events import Event, EventBus, EventType
from omniimmune.memory import ImmuneMemory, UntrustedImmuneMemoryError
from omniimmune.models import (
    AttackDNA,
    AttackType,
    BugClass,
    IncidentSignature,
    ImmunitySummary,
)
from omniimmune.report import ReportGenerator, generate_html, generate_pr_template, generate_prometheus_metrics


def test_attack_dna_defaults_and_hash_exclude_metadata() -> None:
    dna = AttackDNA(
        attack_type=AttackType.FUZZ,
        target_endpoint="/items",
        payload={"z": 1, "a": [2, 3]},
    )
    assert dna.dna_id.startswith("dna-")
    assert dna.verify_integrity()

    changed_metadata = dna.model_copy(update={"dna_id": "another-id", "sha256": ""})
    assert changed_metadata.compute_hash() == dna.compute_hash()

    canonical = (
        '{"attack_type":"fuzz","concurrency":1,"delay_seconds":0.0,'
        '"headers":{},"http_method":"GET","payload":{"a":[2,3],"z":1},'
        '"query_params":{},"raw_body":null,"request_count":1,"seed":1337,'
        '"target_endpoint":"/items"}'
    )
    assert dna.sha256 == hashlib.sha256(canonical.encode()).hexdigest()


def test_attack_dna_integrity_and_file_roundtrip(tmp_path) -> None:
    path = tmp_path / "nested" / "attack.json"
    dna = AttackDNA(attack_type=AttackType.LATENCY, target_endpoint="/health")
    dna.to_file(path)
    loaded = AttackDNA.from_file(path)
    assert loaded == dna
    assert loaded.verify_integrity()

    path.write_text(
        path.read_text(encoding="utf-8").replace('"seed":1337', '"seed":1338'),
        encoding="utf-8",
    )
    assert not AttackDNA.from_file(path).verify_integrity()


def test_incident_signature_id_uses_only_identity_fields() -> None:
    first_id = IncidentSignature.make_sig_id(BugClass.PANIC, "/items", "ValueError")
    same_id = IncidentSignature.make_sig_id(BugClass.PANIC, "/items", "ValueError")
    different_id = IncidentSignature.make_sig_id(BugClass.PANIC, "/other", "ValueError")
    assert first_id == same_id
    assert first_id != different_id

    signature = IncidentSignature(
        sig_id=first_id,
        bug_class=BugClass.PANIC,
        failure_kind="panic",
        endpoint="/items",
        exception_type="ValueError",
        stack_trace="line 10",
    )
    assert signature.sig_id == first_id


def test_immunity_summary_formula_and_empty_run() -> None:
    summary = ImmunitySummary.compute(
        incidents_total=10,
        incidents_verified=4,
        regression_passed=True,
        mean_survival_rate=0.5,
    )
    assert summary.score == 54
    empty = ImmunitySummary.compute(incidents_total=0)
    assert empty.score == 0


def test_immunity_summary_formats_execution_duration() -> None:
    summary = ImmunitySummary.compute(
        incidents_total=1,
        incidents_verified=1,
        regression_passed=True,
        mean_survival_rate=1.0,
    ).model_copy(update={"execution_elapsed_seconds": 84})

    assert summary.formatted_duration() == "1m 24s"


def test_immunity_summary_returns_status_badge() -> None:
    vaccinated = ImmunitySummary.compute(
        incidents_total=1,
        incidents_verified=1,
        regression_passed=True,
        mean_survival_rate=1.0,
    )
    at_risk = vaccinated.model_copy(update={"score": 80})

    assert vaccinated.summary_badge() == "[VACCINATED 100/100]"
    assert at_risk.summary_badge() == "[AT RISK 80/100]"


def test_immunity_summary_engine_version_and_full_vaccination() -> None:
    vaccinated = ImmunitySummary.compute(
        incidents_total=1,
        incidents_verified=1,
        regression_passed=True,
        mean_survival_rate=1.0,
    )

    assert ImmunitySummary.ENGINE_VERSION == "1.0.0"
    assert vaccinated.is_fully_vaccinated()
    assert vaccinated.model_copy(update={"score": 99}).is_fully_vaccinated() is False


@pytest.mark.asyncio
async def test_event_bus_order_wildcard_history_and_isolation(caplog) -> None:
    bus = EventBus()
    received: list[str] = []

    def failing(event: Event) -> None:
        received.append("failing")
        raise RuntimeError("expected callback failure")

    bus.subscribe(EventType.ATTACK_STARTED, failing)
    bus.subscribe("*", lambda event: received.append("wildcard"))
    bus.subscribe(EventType.ATTACK_STARTED, lambda event: received.append("exact"))

    with caplog.at_level("ERROR"):
        event = await bus.publish(EventType.ATTACK_STARTED, {"n": 1})

    assert received == ["failing", "wildcard", "exact"]
    assert event.type == EventType.ATTACK_STARTED
    assert event.data == {"n": 1}
    assert bus.history == [event]
    assert "Event subscriber failed" in caplog.text


@pytest.mark.asyncio
async def test_event_bus_awaits_async_callbacks_in_order() -> None:
    bus = EventBus()
    received: list[str] = []

    async def first(event: Event) -> None:
        await asyncio.sleep(0)
        received.append("first")

    async def second(event: Event) -> None:
        received.append("second")

    bus.subscribe("custom", first)
    bus.subscribe("custom", second)
    await bus.publish("custom", {})
    assert received == ["first", "second"]


def test_immune_memory_roundtrip_corrupt_missing_count_and_clear(tmp_path) -> None:
    memory = ImmuneMemory(root=tmp_path / "memory")
    sig_id = IncidentSignature.make_sig_id(BugClass.STARVATION, "/work", "TimeoutError")
    source = "def work():\n    return 1\n"
    base_sha256 = hashlib.sha256(b"def work():\n    return 0\n").hexdigest()
    memory.save_patch(
        sig_id,
        {
            "strategy": "cached",
            "diff": "- return 0\n+ return 1\n",
            "modified_source": source,
            "base_file_sha256": base_sha256,
            "verified": True,
        },
    )
    cached = memory.get_patch(sig_id)
    assert cached is not None
    assert cached["modified_source"] == source
    assert cached["base_file_sha256"] == base_sha256
    assert isinstance(cached["signature"], str)
    assert memory.get_vaccinated_count() == 1

    (memory.root / f"{sig_id}.json").write_text("{not json", encoding="utf-8")
    assert memory.get_patch(sig_id) is None
    assert memory.get_patch("missing") is None
    assert memory.clear() == 1
    assert memory.get_vaccinated_count() == 0


def test_immune_memory_valid_signature_and_base_sha_check(tmp_path) -> None:
    memory = ImmuneMemory(tmp_path / "memory")
    sig_id = "sig-valid"
    base_sha256 = hashlib.sha256(b"original").hexdigest()
    payload = {
        "strategy": "input_guard",
        "base_file_sha256": base_sha256,
        "modified_source": "hardened",
    }
    path = memory.save_patch(sig_id, payload)
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert memory.get_patch(sig_id, expected_base_file_sha256=base_sha256) == stored
    assert stored["signature"] == memory._sign(
        sig_id, payload["strategy"], base_sha256, payload["modified_source"]
    )

    with pytest.raises(UntrustedImmuneMemoryError):
        memory.get_patch(sig_id, expected_base_file_sha256=hashlib.sha256(b"other").hexdigest())


def test_immune_memory_rejects_modified_source_tampering(tmp_path) -> None:
    memory = ImmuneMemory(tmp_path / "memory")
    sig_id = "sig-tampered"
    memory.save_patch(
        sig_id,
        {
            "strategy": "input_guard",
            "base_file_sha256": hashlib.sha256(b"original").hexdigest(),
            "modified_source": "hardened",
        },
    )
    path = memory.root / f"{sig_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["modified_source"] = "malicious"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(UntrustedImmuneMemoryError):
        memory.get_patch(sig_id)


def test_immune_memory_rejects_tampered_base_sha256(tmp_path) -> None:
    memory = ImmuneMemory(tmp_path / "memory")
    sig_id = "sig-base-tampered"
    memory.save_patch(
        sig_id,
        {
            "strategy": "input_guard",
            "base_file_sha256": hashlib.sha256(b"original").hexdigest(),
            "modified_source": "hardened",
        },
    )
    path = memory.root / f"{sig_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["base_file_sha256"] = hashlib.sha256(b"different").hexdigest()
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(UntrustedImmuneMemoryError):
        memory.get_patch(sig_id)


def test_immune_memory_rejects_tampered_signature(tmp_path) -> None:
    memory = ImmuneMemory(tmp_path / "memory")
    sig_id = "sig-signature-tampered"
    memory.save_patch(
        sig_id,
        {
            "strategy": "input_guard",
            "base_file_sha256": hashlib.sha256(b"original").hexdigest(),
            "modified_source": "hardened",
        },
    )
    path = memory.root / f"{sig_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["signature"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(UntrustedImmuneMemoryError):
        memory.get_patch(sig_id)


def test_immune_memory_rejects_unsafe_signature_ids(tmp_path) -> None:
    memory = ImmuneMemory(tmp_path)
    for unsafe in ("../escape", "a/b", "", ".", "space here"):
        with pytest.raises(ValueError):
            memory.save_patch(unsafe, {})


def test_postmortem_dashboard_is_standalone_and_contains_incident_cards(tmp_path) -> None:
    summary = ImmunitySummary.compute(
        incidents_total=1,
        incidents_verified=1,
        regression_passed=True,
        mean_survival_rate=1.0,
    ).model_copy(
        update={
            "incidents": [
                {
                    "sig_id": "sig-demo123",
                    "bug_class": "panic",
                    "culprit": "target_app/app.py:37",
                    "before": {"p99_ms": 420.0, "error_rate": 1.0},
                    "after": {"p99_ms": 12.0, "error_rate": 0.0},
                    "verdict": "PASS",
                    "branch": "heal/sig-demo123",
                    "commit": "0123456789abcdef",
                    "diff": "- raise ValueError()\n+ return JSONResponse(status_code=400)",
                    "escalation_passed": True,
                    "mutations_tested": 3,
                }
            ]
        },
    )
    output = tmp_path / "report.html"
    html = generate_html(summary, output)
    assert output.read_text(encoding="utf-8") == html
    assert "#0d1117" in html
    assert "100/100" in html
    assert "target_app/app.py:37" in html
    assert "420.0 ms" in html and "12.0 ms" in html
    assert "<details>" in html and "gh pr create" in html


def test_reports_sanitize_absolute_culprits_and_diff_headers(tmp_path) -> None:
    summary = ImmunitySummary.compute(incidents_total=1, incidents_verified=1).model_copy(
        update={
            "incidents": [
                {
                    "sig_id": "sig-paths",
                    "culprit": "/Users/example/repo/target_app/app.py:37",
                    "diff": "--- a//Users/example/repo/target_app/app.py\n+++ b//Users/example/repo/target_app/app.py\n@@ -1 +1 @@\n",
                    "verdict": "PASS",
                }
            ]
        }
    )
    html = generate_html(summary, tmp_path / "report.html")
    assert "target_app/app.py:37" in html
    assert "/Users/example" not in html
    assert "--- a/target_app/app.py" in html
    assert "+++ b/target_app/app.py" in html


def test_markdown_report_contains_incident_postmortem_section(tmp_path) -> None:
    summary = ImmunitySummary.compute(incidents_total=1, incidents_verified=1).model_copy(
        update={"incidents": [{"sig_id": "sig-report", "culprit": "/tmp/target_app/app.py:37"}]}
    )
    content = ReportGenerator().generate(summary, tmp_path / "INCIDENT_REPORT.md")
    assert "## Executive Summary" in content
    assert "## Event Timeline" in content
    assert "## Incident Post-Mortem" in content
    assert "/tmp/" not in content


def test_prometheus_metrics_export(tmp_path) -> None:
    summary = ImmunitySummary.compute(
        incidents_total=3,
        incidents_verified=2,
        regression_passed=True,
        mean_survival_rate=0.75,
    )
    path = tmp_path / "artifacts" / "metrics.prom"
    content = generate_prometheus_metrics(summary, path)
    assert path.read_text(encoding="utf-8") == content
    assert "# TYPE omniimmune_score gauge\nomniimmune_score 75" in content
    assert "omniimmune_incidents_verified 2" in content
    assert "omniimmune_incidents_total 3" in content
    assert "omniimmune_mean_survival_rate 0.75" in content
    assert "# TYPE omni_immunity_score gauge\nomni_immunity_score 75" in content
    assert "omni_incidents_verified 2" in content
    assert "omni_incidents_total 3" in content
    assert "omni_golden_regression_pass 1" in content
    assert "omni_survival_rate_mean 0.75" in content


def test_pr_template_contains_culprit_metrics_diff_proof_and_checklist(tmp_path) -> None:
    incident = {
        "sig_id": "sig-template",
        "bug_class": "starvation",
        "culprit": "target_app/app.py:72",
        "strategy": "asyncio_lock_nonblocking",
        "before": {"p50_ms": 10.0, "p95_ms": 500.0, "p99_ms": 900.0, "error_rate": 0.5, "survival_rate": 0.5},
        "after": {"p50_ms": 8.0, "p95_ms": 12.0, "p99_ms": 15.0, "error_rate": 0.0, "survival_rate": 1.0},
        "verdict": "PASS",
        "escalation_passed": True,
        "mutations_tested": 3,
        "diff": "- with _inventory_lock:\n+ async with _inventory_lock:",
        "escalation_details": [{"mutation": "numeric_boundary", "survived": True}],
    }
    path = generate_pr_template(incident, tmp_path / "prs")
    content = path.read_text(encoding="utf-8")
    assert path.name == "sig-template.md"
    assert "target_app/app.py:72" in content
    assert "| p99 latency | 900.0 ms | 15.0 ms |" in content
    assert "LibCST" in content and "asyncio_lock_nonblocking" in content
    assert "numeric_boundary" in content and "Approval checklist" in content
