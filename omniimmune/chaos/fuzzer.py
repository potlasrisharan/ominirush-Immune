from __future__ import annotations

import asyncio
import random
import time

import httpx

from omniimmune.models import AttackDNA, AttackType

from .common import response_body


class FuzzNet:
    def __init__(self, seed: int = 1337) -> None:
        self.seed = seed

    def generate_payloads(self) -> list[dict | str]:
        rng = random.Random(self.seed)
        return [
            {"item_id": "item-neg", "quantity": -5},
            {"item_id": "item-zero", "quantity": 0},
            {"item_id": "item-str", "quantity": "invalid_qty"},
            '{"item_id": "broken", "quantity":',
            {"item_id": "overflow", "quantity": 9223372036854775807},
            {"item_id": "' OR 1=1; DROP TABLE orders;--", "quantity": 1},
            {"item_id": "A" * rng.randint(2000, 5000), "quantity": 1},
            {"item_id": "item-rand", "quantity": -rng.randint(1, 10**9)},
        ]

    def build_dna(self, payload_idx: int = 0) -> AttackDNA:
        payload = self.generate_payloads()[payload_idx]
        return AttackDNA(
            attack_type=AttackType.FUZZ,
            target_endpoint="/api/v1/order",
            http_method="POST",
            payload=None if isinstance(payload, str) else payload,
            raw_body=payload if isinstance(payload, str) else None,
            headers={"Content-Type": "application/json"},
            seed=self.seed,
        )

    async def execute(
        self,
        target_url: str,
        dna: AttackDNA,
        client: httpx.AsyncClient | None = None,
        request_timeout: float = 5.0,
    ) -> dict:
        owned_client = client is None
        active_client = client if client is not None else httpx.AsyncClient()
        started = time.perf_counter()
        try:
            url = f"{target_url.rstrip('/')}{dna.target_endpoint}"
            request_kwargs = {"headers": dna.headers, "params": dna.query_params}
            if dna.raw_body is not None:
                request_kwargs["content"] = dna.raw_body
            else:
                request_kwargs["json"] = dna.payload
            response = await asyncio.wait_for(
                active_client.request(dna.http_method, url, **request_kwargs),
                timeout=request_timeout,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            status_code = int(response.status_code)
            is_failure = status_code >= 500
            return {
                "status_code": status_code,
                "response_body": response_body(response),
                "elapsed_ms": elapsed_ms,
                "is_failure": is_failure,
                "failure_kind": "exception" if is_failure else None,
                "error": None,
            }
        except asyncio.TimeoutError:
            return {
                "status_code": 0,
                "response_body": "",
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "is_failure": True,
                "failure_kind": "hang",
                "error": "request timed out",
            }
        except httpx.TimeoutException as exc:
            return {
                "status_code": 0,
                "response_body": "",
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "is_failure": True,
                "failure_kind": "hang",
                "error": str(exc) or exc.__class__.__name__,
            }
        except httpx.TransportError as exc:
            return {
                "status_code": 0,
                "response_body": "",
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "is_failure": True,
                "failure_kind": "crash",
                "error": str(exc) or exc.__class__.__name__,
            }
        finally:
            if owned_client:
                await active_client.aclose()

    async def run(
        self, target_url: str, client: httpx.AsyncClient | None = None
    ) -> tuple[AttackDNA, dict]:
        last: tuple[AttackDNA, dict] | None = None
        for payload_idx in range(len(self.generate_payloads())):
            dna = self.build_dna(payload_idx)
            result = await self.execute(target_url, dna, client=client)
            result["swept"] = payload_idx + 1
            last = (dna, result)
            if result["is_failure"]:
                return last
        assert last is not None
        return last
