from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field, model_validator


class AttackType(str, Enum):
    FUZZ = "fuzz"
    STORM = "storm"
    LATENCY = "latency"
    LEAK = "leak"


class BugClass(str, Enum):
    PANIC = "panic"
    STARVATION = "starvation"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    RESOURCE_EXHAUSTION = "resource_exhaustion"


class AttackDNA(BaseModel):
    dna_id: str = ""
    attack_type: AttackType
    target_endpoint: str
    http_method: str = "GET"
    payload: dict | list | str | None = None
    raw_body: str | None = None
    query_params: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    concurrency: int = 1
    request_count: int = 1
    delay_seconds: float = 0.0
    seed: int = 1337
    sha256: str = ""

    def compute_hash(self) -> str:
        canonical = self.model_dump(mode="json", exclude={"sha256", "dna_id"})
        serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @model_validator(mode="after")
    def fill_identity(self) -> "AttackDNA":
        if not self.sha256:
            self.sha256 = self.compute_hash()
        if not self.dna_id:
            self.dna_id = f"dna-{self.sha256[:12]}"
        return self

    def verify_integrity(self) -> bool:
        return bool(self.sha256) and self.sha256 == self.compute_hash()

    def to_file(self, path: Path | str) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    @classmethod
    def from_file(cls, path: Path | str) -> "AttackDNA":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class LatencyStats(BaseModel):
    p50_ms: float
    p95_ms: float
    p99_ms: float
    error_rate: float
    survival_rate: float
    peak_mem_mb: float


class TelemetrySample(BaseModel):
    ts: float
    container: str
    cpu_percent: float
    mem_mb: float
    mem_limit_mb: float | None = None


class IncidentSignature(BaseModel):
    sig_id: str
    bug_class: BugClass
    failure_kind: str
    endpoint: str
    exception_type: str | None = None
    message_token: str | None = None
    culprit_file: str | None = None
    culprit_function: str | None = None
    culprit_line: int | None = None
    stack_trace: str | None = None
    telemetry: list[TelemetrySample] = Field(default_factory=list)
    dna_id: str = ""

    @classmethod
    def make_sig_id(
        cls,
        bug_class: BugClass | str,
        endpoint: str,
        exception_type: str | None,
    ) -> str:
        bug_value = bug_class.value if isinstance(bug_class, BugClass) else str(bug_class)
        identity = json.dumps(
            [bug_value, endpoint, exception_type], separators=(",", ":"), ensure_ascii=False
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"sig-{digest[:12]}"


class PatchResult(BaseModel):
    sig_id: str
    strategy: str
    file_path: str
    diff: str
    applied: bool
    from_memory: bool = False
    error: str | None = None
    base_file_sha256: str = ""


class VerificationVerdict(BaseModel):
    sig_id: str
    dna_id: str
    passed: bool
    status_codes: dict[str, int] = Field(default_factory=dict)
    before: LatencyStats | None = None
    after: LatencyStats
    regression_passed: bool
    rolled_back: bool = False
    strategy_used: str = ""
    dna_sha256: str = ""
    escalation_passed: bool | None = None
    mutations_tested: int = 0


class ImmunitySummary(BaseModel):
    score: int
    incidents_total: int
    incidents_verified: int
    regression_passed: bool
    mean_survival_rate: float
    vaccinated_count: int
    incidents: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    patch_diffs: list[str] = Field(default_factory=list)
    # Generated artifact locations are kept on the returned summary so callers
    # can link directly to the dashboard and reviewer-ready PR bodies.
    report_path: str | None = None
    html_report_path: str | None = None
    pr_template_paths: list[str] = Field(default_factory=list)

    @classmethod
    def compute(
        cls,
        incidents_total: int = 0,
        incidents_verified: int = 0,
        regression_passed: bool = False,
        mean_survival_rate: float | None = None,
        vaccinated_count: int = 0,
        *,
        survival_rates: Iterable[float] | None = None,
        total: int | None = None,
        verified: int | None = None,
    ) -> "ImmunitySummary":
        """Apply score = 60*verified/total + 20*regression + 20*mean_survival."""
        if total is not None:
            incidents_total = total
        if verified is not None:
            incidents_verified = verified
        if survival_rates is not None:
            rates = list(survival_rates)
            mean_survival_rate = sum(rates) / len(rates) if rates else 0.0
        if mean_survival_rate is None:
            mean_survival_rate = 0.0
        verification_rate = incidents_verified / incidents_total if incidents_total else 0.0
        score = round(
            60 * verification_rate
            + 20 * (1 if regression_passed else 0)
            + 20 * mean_survival_rate
        )
        return cls(
            score=score,
            incidents_total=incidents_total,
            incidents_verified=incidents_verified,
            regression_passed=regression_passed,
            mean_survival_rate=float(mean_survival_rate),
            vaccinated_count=vaccinated_count,
        )
