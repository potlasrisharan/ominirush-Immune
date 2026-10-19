from __future__ import annotations

import asyncio
from pathlib import Path

from omniimmune.events import EventBus, EventType
from omniimmune.models import AttackDNA, AttackType

from .common import ATTACK_BUG_HINT
from .fuzzer import FuzzNet
from .latency import LatencyDriftAgent
from .storm import StormLoad


class RedSwarmRunner:
    def __init__(
        self,
        target_url: str,
        event_bus: EventBus,
        artifacts_dir: str = "artifacts/dna",
        seed: int = 1337,
        storm_concurrency: int = 50,
        storm_requests: int = 50,
        latency_delay: float = 4.0,
        client_timeout: float = 2.0,
        sla_ms: float = 1500.0,
    ) -> None:
        self.target_url = target_url
        self.event_bus = event_bus
        self.artifacts_dir = artifacts_dir
        self.seed = seed
        self.storm_concurrency = storm_concurrency
        self.storm_requests = storm_requests
        self.latency_delay = latency_delay
        self.client_timeout = client_timeout
        self.sla_ms = sla_ms

    @staticmethod
    def _endpoint(attack_type: AttackType) -> str:
        return {
            AttackType.FUZZ: "/api/v1/order",
            AttackType.STORM: "/api/v1/inventory/reserve",
            AttackType.LATENCY: "/api/v1/payment/verify",
        }[attack_type]

    async def run_vector(
        self, attack_type: AttackType | str, client=None
    ) -> tuple[AttackDNA, dict]:
        vector = AttackType(attack_type)
        endpoint = self._endpoint(vector)
        await self.event_bus.publish(
            EventType.ATTACK_STARTED,
            {"attack_type": vector.value, "target_endpoint": endpoint},
        )

        if vector is AttackType.FUZZ:
            dna, result = await FuzzNet(seed=self.seed).run(self.target_url, client=client)
        elif vector is AttackType.STORM:
            dna, result = await StormLoad(
                seed=self.seed, default_concurrency=self.storm_concurrency
            ).run(
                self.target_url,
                client=client,
                concurrency=self.storm_concurrency,
                request_count=self.storm_requests,
                sla_ms=self.sla_ms,
            )
        else:
            dna, result = await LatencyDriftAgent().run(
                self.target_url,
                client=client,
                delay_seconds=self.latency_delay,
                client_timeout=self.client_timeout,
                sla_ms=self.sla_ms,
            )

        dna.to_file(Path(self.artifacts_dir) / f"{dna.dna_id}.json")
        if result["is_failure"]:
            await self.event_bus.publish(
                EventType.FAILURE_DETECTED,
                {
                    "dna_id": dna.dna_id,
                    "attack_type": vector.value,
                    "bug_class_hint": ATTACK_BUG_HINT[vector].value,
                    "endpoint": dna.target_endpoint,
                    "failure_kind": result["failure_kind"],
                    "status_code": result["status_code"],
                    "elapsed_ms": result["elapsed_ms"],
                    "error": result["error"],
                },
            )
        await self.event_bus.publish(
            EventType.ATTACK_FINISHED,
            {"dna_id": dna.dna_id, "attack_type": vector.value, "is_failure": result["is_failure"]},
        )
        return dna, result

    async def run_all_parallel(self, client=None) -> list[tuple[AttackDNA, dict]]:
        return list(
            await asyncio.gather(
                *(self.run_vector(vector, client=client) for vector in AttackType)
            )
        )
