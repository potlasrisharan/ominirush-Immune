from __future__ import annotations

import asyncio
import os
import time
from collections import Counter
from pathlib import Path

import psutil

import httpx

from omniimmune.models import AttackDNA, AttackType

from .common import compute_latency_stats, response_body


class MemoryStressAgent:
    """Deterministically exercise a cache and identify unbounded growth.

    The target reports its retained entry count in each response.  Comparing
    the first and final counts avoids relying on process-specific RSS values,
    which keeps this vector useful against both an in-process ASGI target and a
    separately running container.
    """

    def __init__(
        self,
        seed: int = 1337,
        default_request_count: int = 256,
        default_payload_size: int = 4096,
        concurrency: int = 8,
    ) -> None:
        self._validate_positive(default_request_count, "default_request_count")
        self._validate_positive(default_payload_size, "default_payload_size")
        self._validate_positive(concurrency, "concurrency")
        self.seed = seed
        self.default_request_count = default_request_count
        self.default_payload_size = default_payload_size
        self.concurrency = concurrency

    @staticmethod
    def _validate_positive(value: int, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")

    @staticmethod
    def _rss_mb() -> float:
        try:
            return psutil.Process(os.getpid()).memory_info().rss / 1_000_000
        except (OSError, AttributeError):
            return 0.0

    @staticmethod
    def _memory_limit_mb() -> float | None:
        for filename in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            try:
                value = Path(filename).read_text(encoding="utf-8").strip()
                if value and value != "max":
                    limit = int(value) / 1_000_000
                    if limit > 0:
                        return limit
            except (OSError, ValueError):
                continue
        return None

    def build_dna(
        self,
        request_count: int | None = None,
        payload_size: int | None = None,
    ) -> AttackDNA:
        count = self.default_request_count if request_count is None else request_count
        size = self.default_payload_size if payload_size is None else payload_size
        self._validate_positive(count, "request_count")
        self._validate_positive(size, "payload_size")
        return AttackDNA(
            attack_type=AttackType.LEAK,
            target_endpoint="/api/v1/cache/item",
            http_method="POST",
            payload={"data": "x" * size},
            headers={"Content-Type": "application/json"},
            concurrency=self.concurrency,
            request_count=count,
            seed=self.seed,
        )

    async def execute(
        self,
        target_url: str,
        dna: AttackDNA,
        client: httpx.AsyncClient | None = None,
        request_timeout: float = 5.0,
    ) -> dict:
        self._validate_positive(dna.request_count, "DNA request_count")
        self._validate_positive(dna.concurrency, "DNA concurrency")
        owned_client = client is None
        active_client = client if client is not None else httpx.AsyncClient()
        started = time.perf_counter()
        rss_before_mb = self._rss_mb()
        rss_samples: list[float] = [rss_before_mb]
        memory_limit_mb = self._memory_limit_mb()
        try:
            payload_size = 1024
            if isinstance(dna.payload, dict) and isinstance(dna.payload.get("data"), str):
                payload_size = max(1, len(dna.payload["data"]))
            semaphore = asyncio.Semaphore(dna.concurrency)
            request_results: list[tuple[int, float, str, int | None]] = []

            async def one_request(index: int) -> tuple[int, float, str, int | None]:
                async with semaphore:
                    request_started = time.perf_counter()
                    key = f"stress-{dna.seed}-{index}"
                    params = dict(dna.query_params)
                    params["key"] = key
                    request_payload = dict(dna.payload) if isinstance(dna.payload, dict) else dna.payload
                    if isinstance(request_payload, dict):
                        request_payload["key"] = key
                    try:
                        response = await asyncio.wait_for(
                            active_client.request(
                                dna.http_method,
                                f"{target_url.rstrip('/')}{dna.target_endpoint}",
                                headers=dna.headers,
                                params=params,
                                json=request_payload,
                            ),
                            timeout=request_timeout,
                        )
                        body = response_body(response)
                        rss_samples.append(self._rss_mb())
                        cache_count: int | None = None
                        try:
                            payload = response.json()
                            count = payload.get("count", payload.get("cache_size"))
                            cache_count = int(count)
                        except (AttributeError, TypeError, ValueError):
                            pass
                        return (
                            int(response.status_code),
                            (time.perf_counter() - request_started) * 1000,
                            body,
                            cache_count,
                        )
                    except (asyncio.TimeoutError, httpx.TimeoutException):
                        return 0, request_timeout * 1000, "", None
                    except httpx.TransportError:
                        return 0, (time.perf_counter() - request_started) * 1000, "", None

            request_results = list(
                await asyncio.gather(*(one_request(index) for index in range(dna.request_count)))
            )
        finally:
            if owned_client:
                await active_client.aclose()

        statuses = [result[0] for result in request_results]
        durations = [result[1] for result in request_results]
        counts = [result[3] for result in request_results if result[3] is not None]
        initial_count = counts[0] - 1 if counts else 0
        final_count = counts[-1] if counts else initial_count
        successful = sum(status < 500 and status != 0 for status in statuses)
        grew_for_every_request = bool(counts) and final_count - initial_count >= successful
        rss_peak_mb = max(rss_samples or [0.0])
        estimated_peak_mb = (final_count * payload_size) / 1_000_000
        peak_mem_mb = max(estimated_peak_mb, rss_peak_mb)
        memory_limit_exceeded = bool(
            memory_limit_mb is not None and peak_mem_mb >= memory_limit_mb * 0.9
        )
        stats = compute_latency_stats(
            durations,
            statuses,
            peak_mem_mb=peak_mem_mb,
        )
        status_counts = Counter(statuses)
        status_code = status_counts.most_common(1)[0][0] if status_counts else 0
        is_failure = grew_for_every_request or stats.error_rate > 0.05 or memory_limit_exceeded
        return {
            "status_code": status_code,
            "response_body": next((result[2] for result in request_results if result[2]), "")[:2000],
            "elapsed_ms": stats.p99_ms or ((time.perf_counter() - started) * 1000),
            "is_failure": is_failure,
            "failure_kind": "resource_exhaustion" if grew_for_every_request or memory_limit_exceeded else "exception" if stats.error_rate > 0.05 else None,
            "error": (
                f"cache grew by {final_count - initial_count} entries"
                if grew_for_every_request
                else f"RSS approached memory limit ({rss_peak_mb:.1f}MB/{memory_limit_mb:.1f}MB)"
                if memory_limit_exceeded and memory_limit_mb is not None
                else f"error rate {stats.error_rate:.1%} exceeded 5%"
                if stats.error_rate > 0.05
                else None
            ),
            "stats": stats,
            "status_codes": {str(status): count for status, count in sorted(status_counts.items())},
            "initial_cache_size": initial_count,
            "final_cache_size": final_count,
            "cache_growth": final_count - initial_count,
            "rss_before_mb": rss_before_mb,
            "rss_peak_mb": rss_peak_mb,
            "memory_limit_mb": memory_limit_mb,
            "memory_limit_exceeded": memory_limit_exceeded,
        }

    async def run(
        self,
        target_url: str,
        client: httpx.AsyncClient | None = None,
        *,
        request_count: int | None = None,
        payload_size: int | None = None,
        request_timeout: float = 5.0,
    ) -> tuple[AttackDNA, dict]:
        dna = self.build_dna(request_count=request_count, payload_size=payload_size)
        return dna, await self.execute(target_url, dna, client=client, request_timeout=request_timeout)
