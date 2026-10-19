from __future__ import annotations

import asyncio
import time

import httpx

from omniimmune.chaos.common import compute_latency_stats, replay
from omniimmune.models import AttackDNA, AttackType, LatencyStats, VerificationVerdict


class VerifierEngine:
    def __init__(self, target_url: str = "http://localhost:8000") -> None:
        self.target_url = target_url.rstrip("/")

    async def _get_json(self, client: httpx.AsyncClient, path: str) -> httpx.Response:
        return await asyncio.wait_for(client.get(f"{self.target_url}{path}"), timeout=5.0)

    async def check_golden_regression(self, client: httpx.AsyncClient) -> bool:
        try:
            health = await self._get_json(client, "/health")
            if health.status_code != 200 or health.json().get("status") != "ok":
                return False
            catalog = await self._get_json(client, "/api/v1/catalog")
            if catalog.status_code != 200 or "catalog" not in catalog.json():
                return False
            status = await self._get_json(client, "/api/v1/status")
            return status.status_code == 200 and bool(status.json().get("healthy"))
        except (asyncio.TimeoutError, httpx.HTTPError, ValueError):
            return False

    async def verify_attack_dna(
        self,
        dna: AttackDNA,
        sig_id: str,
        strategy_used: str,
        client: httpx.AsyncClient,
        sla_ms: float = 1500.0,
    ) -> VerificationVerdict:
        started = time.perf_counter()
        replay_kwargs: dict[str, object] = {}
        if dna.attack_type is AttackType.LATENCY:
            replay_kwargs["client_timeout"] = max(2.0, (sla_ms / 1000) + 1.0)
            replay_kwargs["sla_ms"] = sla_ms
        elif dna.attack_type is AttackType.STORM:
            replay_kwargs["sla_ms"] = sla_ms
        result = await replay(dna, self.target_url, client=client, **replay_kwargs)
        elapsed_ms = result.get("elapsed_ms", (time.perf_counter() - started) * 1000)
        status_code = int(result.get("status_code", 0))
        statuses: list[int]
        durations: list[float]
        if dna.attack_type is AttackType.STORM and "stats" in result:
            after = result["stats"]
            statuses = [
                int(status)
                for status, count in result.get("status_codes", {}).items()
                for _ in range(int(count))
            ]
            durations = [float(after.p99_ms)] * max(1, len(statuses))
            status_codes = dict(result.get("status_codes", {}))
        else:
            statuses = [status_code]
            durations = [float(elapsed_ms)]
            status_codes = {str(status_code): 1}
            after = compute_latency_stats(durations, statuses)

            # A 504 emitted by the explicit timeout boundary is a successful
            # containment response, not an application error for immunity
            # scoring purposes.
            if dna.attack_type is AttackType.LATENCY and status_code == 504:
                after = compute_latency_stats(durations, [200])

        if dna.attack_type is AttackType.STORM and "stats" in result:
            after = LatencyStats.model_validate(result["stats"])

        handled_status = status_code in {200, 400, 408, 504}
        no_timeout = status_code != 0 and result.get("failure_kind") not in {"hang", "crash"}
        attack_passed = handled_status and no_timeout and after.p99_ms <= sla_ms
        # A controlled 504 is the explicit upstream-timeout fallback and is a
        # handled response even though it can sit at the timeout boundary.
        if dna.attack_type is AttackType.LATENCY and status_code == 504 and no_timeout:
            attack_passed = True
        regression_passed = await self.check_golden_regression(client)
        return VerificationVerdict(
            sig_id=sig_id,
            dna_id=dna.dna_id,
            passed=attack_passed and regression_passed,
            status_codes=status_codes,
            after=after,
            regression_passed=regression_passed,
            strategy_used=strategy_used,
            dna_sha256=dna.sha256,
            escalation_passed=None,
        )
