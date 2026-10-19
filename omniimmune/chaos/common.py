from __future__ import annotations

import json
from math import ceil
from typing import Iterable

from omniimmune.models import AttackType, BugClass, LatencyStats


ATTACK_BUG_HINT = {
    AttackType.FUZZ: BugClass.PANIC,
    AttackType.STORM: BugClass.STARVATION,
    AttackType.LATENCY: BugClass.UPSTREAM_TIMEOUT,
}


def compute_latency_stats(
    durations_ms: Iterable[float],
    statuses: Iterable[int],
    peak_mem_mb: float = 0.0,
) -> LatencyStats:
    durations = sorted(float(duration) for duration in durations_ms)
    status_values = [int(status) for status in statuses]
    if not durations:
        return LatencyStats(
            p50_ms=0.0,
            p95_ms=0.0,
            p99_ms=0.0,
            error_rate=0.0,
            survival_rate=0.0,
            peak_mem_mb=float(peak_mem_mb),
        )

    def nearest_rank(percentile: float) -> float:
        index = max(0, ceil(percentile / 100 * len(durations)) - 1)
        return durations[index]

    errors = sum(status == 0 or status >= 500 for status in status_values)
    error_rate = errors / len(status_values) if status_values else 0.0
    return LatencyStats(
        p50_ms=nearest_rank(50),
        p95_ms=nearest_rank(95),
        p99_ms=nearest_rank(99),
        error_rate=error_rate,
        survival_rate=1.0 - error_rate,
        peak_mem_mb=float(peak_mem_mb),
    )


def response_body(response: object) -> str:
    """Extract a bounded response body from real and lightweight test responses."""
    text = getattr(response, "text", None)
    if text is not None:
        return str(text)[:2000]
    json_method = getattr(response, "json", None)
    if callable(json_method):
        try:
            return json.dumps(json_method(), separators=(",", ":"))[:2000]
        except Exception:
            pass
    return ""


async def replay(dna, target_url: str, client=None, **kwargs) -> dict:
    """Replay a saved DNA through the same implementation used by the attack."""
    from .fuzzer import FuzzNet
    from .latency import LatencyDriftAgent
    from .storm import StormLoad

    attack_type = AttackType(dna.attack_type)
    if attack_type is AttackType.FUZZ:
        return await FuzzNet(seed=dna.seed).execute(target_url, dna, client=client, **kwargs)
    if attack_type is AttackType.STORM:
        return await StormLoad(seed=dna.seed).execute(target_url, dna, client=client, **kwargs)
    if attack_type is AttackType.LATENCY:
        return await LatencyDriftAgent().execute(target_url, dna, client=client, **kwargs)
    raise ValueError(f"Unsupported attack type: {attack_type}")
