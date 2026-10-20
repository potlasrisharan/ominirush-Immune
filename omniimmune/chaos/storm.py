from __future__ import annotations

import asyncio
import time
from collections import Counter

import httpx

from omniimmune.models import AttackDNA, AttackType

from .common import compute_latency_stats, response_body


class StormLoad:
    def __init__(self, seed: int = 1337, default_concurrency: int = 50) -> None:
        if (
            isinstance(default_concurrency, bool)
            or not isinstance(default_concurrency, int)
            or default_concurrency < 1
        ):
            raise ValueError("default_concurrency must be a positive integer")
        self.seed = seed
        self.default_concurrency = default_concurrency

    def build_dna(
        self, concurrency: int | None = None, request_count: int | None = None
    ) -> AttackDNA:
        actual_concurrency = concurrency if concurrency is not None else self.default_concurrency
        actual_count = request_count if request_count is not None else self.default_concurrency
        if (
            isinstance(actual_concurrency, bool)
            or not isinstance(actual_concurrency, int)
            or actual_concurrency < 1
        ):
            raise ValueError("concurrency must be a positive integer")
        if (
            isinstance(actual_count, bool)
            or not isinstance(actual_count, int)
            or actual_count < 1
        ):
            raise ValueError("request_count must be a positive integer")
        return AttackDNA(
            attack_type=AttackType.STORM,
            target_endpoint="/api/v1/inventory/reserve",
            http_method="POST",
            headers={},
            concurrency=actual_concurrency,
            request_count=actual_count,
            seed=self.seed,
        )

    async def execute(
        self,
        target_url: str,
        dna: AttackDNA,
        client: httpx.AsyncClient | None = None,
        sla_ms: float = 1500.0,
        request_timeout: float = 30.0,
    ) -> dict:
        if (
            isinstance(dna.concurrency, bool)
            or not isinstance(dna.concurrency, int)
            or dna.concurrency < 1
        ):
            raise ValueError("DNA concurrency must be a positive integer")
        if (
            isinstance(dna.request_count, bool)
            or not isinstance(dna.request_count, int)
            or dna.request_count < 1
        ):
            raise ValueError("DNA request_count must be a positive integer")
        owned_client = client is None
        active_client = client if client is not None else httpx.AsyncClient()
        semaphore = asyncio.Semaphore(dna.concurrency)

        async def one_request() -> tuple[int, float, str]:
            async with semaphore:
                started = time.perf_counter()
                try:
                    url = f"{target_url.rstrip('/')}{dna.target_endpoint}"
                    response = await asyncio.wait_for(
                        active_client.request(
                            dna.http_method,
                            url,
                            headers=dna.headers,
                            json=dna.payload,
                            params=dna.query_params,
                        ),
                        timeout=request_timeout,
                    )
                    return (
                        int(response.status_code),
                        (time.perf_counter() - started) * 1000,
                        response_body(response),
                    )
                except (asyncio.TimeoutError, httpx.TimeoutException):
                    return 0, request_timeout * 1000, ""
                except httpx.TransportError:
                    return 0, (time.perf_counter() - started) * 1000, ""

        try:
            request_results = await asyncio.gather(
                *(one_request() for _ in range(dna.request_count))
            )
        finally:
            if owned_client:
                await active_client.aclose()

        statuses = [result[0] for result in request_results]
        durations = [result[1] for result in request_results]
        stats = compute_latency_stats(durations, statuses, peak_mem_mb=0.0)
        status_counts = Counter(statuses)
        status_code = status_counts.most_common(1)[0][0] if status_counts else 0
        error_rate_breach = stats.error_rate > 0.05
        sla_breach = stats.p99_ms > sla_ms
        is_failure = sla_breach or error_rate_breach
        failure_kind = "hang" if sla_breach else "exception" if error_rate_breach else None
        representative_body = next(
            (body for status, _, body in request_results if status == status_code and body), ""
        )
        return {
            "status_code": status_code,
            "response_body": representative_body[:2000],
            "elapsed_ms": stats.p99_ms,
            "is_failure": is_failure,
            "failure_kind": failure_kind,
            "error": (
                f"p99 latency {stats.p99_ms:.1f}ms exceeded SLA {sla_ms:.1f}ms"
                if sla_breach
                else f"error rate {stats.error_rate:.1%} exceeded 5%"
                if error_rate_breach
                else None
            ),
            "stats": stats,
            "status_codes": {str(status): count for status, count in sorted(status_counts.items())},
        }

    async def run(
        self,
        target_url: str,
        client: httpx.AsyncClient | None = None,
        *,
        concurrency: int | None = None,
        request_count: int | None = None,
        sla_ms: float = 1500.0,
        request_timeout: float = 30.0,
    ) -> tuple[AttackDNA, dict]:
        dna = self.build_dna(concurrency=concurrency, request_count=request_count)
        return dna, await self.execute(
            target_url,
            dna,
            client=client,
            sla_ms=sla_ms,
            request_timeout=request_timeout,
        )
