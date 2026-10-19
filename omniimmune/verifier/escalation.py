from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any

import httpx

from omniimmune.chaos.common import replay
from omniimmune.models import AttackDNA, AttackType


class EscalationEngine:
    """Run a small, deterministic adversarial generalisation round.

    The engine deliberately keeps the primary mutation count fixed at three.
    A mutation is a request-shape variation for JSON/query endpoints, or a
    bounded workload variation for the bodyless inventory endpoint.
    """

    MUTATION_TIMEOUT_SECONDS = 2.0
    OVERALL_TIMEOUT_SECONDS = 8.0
    HEALTH_TIMEOUT_SECONDS = 1.0
    MAX_CONCURRENCY = 16
    MAX_REQUEST_COUNT = 32

    def __init__(self, seed: int = 42):
        self.seed = seed

    @staticmethod
    def _json_payload(dna: AttackDNA) -> dict[str, Any]:
        if isinstance(dna.payload, dict):
            return copy.deepcopy(dna.payload)
        if dna.raw_body is not None:
            try:
                parsed = json.loads(dna.raw_body)
            except (TypeError, ValueError):
                parsed = {}
            return copy.deepcopy(parsed) if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _build(original: AttackDNA, **changes: Any) -> AttackDNA:
        values = original.model_dump(mode="python")
        values.update(changes)
        # Identity values are derived by AttackDNA's existing validator.  A
        # model_copy(update=...) would retain the old identity, so always use
        # model_validate with both metadata fields empty.
        values["dna_id"] = ""
        values["sha256"] = ""
        values["seed"] = original.seed + 1000
        return AttackDNA.model_validate(values)

    def mutate_dna(self, original_dna: AttackDNA) -> list[AttackDNA]:
        """Return exactly three deterministic, independently identified DNA objects."""
        endpoint = original_dna.target_endpoint.rstrip("/") or "/"
        is_order_json = endpoint == "/api/v1/order" and original_dna.http_method.upper() in {
            "POST",
            "PUT",
            "PATCH",
        }

        if is_order_json:
            first_payload = self._json_payload(original_dna)
            first_payload["quantity"] = -9999999999
            first = self._build(
                original_dna,
                payload=None if original_dna.raw_body is not None else first_payload,
                raw_body=(
                    json.dumps(first_payload, ensure_ascii=False, separators=(",", ":"))
                    if original_dna.raw_body is not None
                    else None
                ),
            )

            second_payload = self._json_payload(original_dna)
            second_payload["quantity"] = "🔥_null_byte_\x00_' OR 1=1 --"
            second = self._build(
                original_dna,
                payload=None if original_dna.raw_body is not None else second_payload,
                raw_body=(
                    json.dumps(second_payload, ensure_ascii=False, separators=(",", ":"))
                    if original_dna.raw_body is not None
                    else None
                ),
            )

            nested_payload = self._json_payload(original_dna)
            nested_payload["quantity"] = {"$nested": {"items": [-1, "unexpected"]}}
            # Keep the nested payload available for inspection while sending a
            # deliberately malformed body to exercise the raw-body path.
            malformed = json.dumps(
                {**nested_payload, "quantity": nested_payload["quantity"]},
                ensure_ascii=False,
                separators=(",", ":"),
            )[:-1]
            third = self._build(original_dna, payload=nested_payload, raw_body=malformed)
            return [first, second, third]

        if original_dna.attack_type is AttackType.STORM or endpoint == "/api/v1/inventory/reserve":
            base_concurrency = max(1, min(original_dna.concurrency, self.MAX_CONCURRENCY))
            base_count = max(1, min(original_dna.request_count, self.MAX_REQUEST_COUNT))
            first = self._build(
                original_dna,
                concurrency=min(self.MAX_CONCURRENCY, max(base_concurrency + 1, 2)),
                request_count=min(self.MAX_REQUEST_COUNT, max(base_count + 1, 2)),
            )
            second = self._build(
                original_dna,
                concurrency=base_concurrency,
                request_count=min(self.MAX_REQUEST_COUNT, max(base_count * 2, base_count + 1)),
            )
            third = self._build(
                original_dna,
                concurrency=min(self.MAX_CONCURRENCY, max(base_concurrency * 2, 2)),
                request_count=base_count,
            )
            return [first, second, third]

        # Query-driven endpoints must be changed through relevant query
        # parameters.  Keep delay values finite so a mutation cannot create an
        # unbounded external workload.
        first_params = dict(original_dna.query_params)
        first_params["delay"] = "0"
        first = self._build(original_dna, query_params=first_params)

        second_params = dict(original_dna.query_params)
        second_params["delay"] = "🔥_null_byte_\x00_' OR 1=1 --"
        second = self._build(original_dna, query_params=second_params)

        third_params = dict(original_dna.query_params)
        third_params["delay"] = '{"$nested":{"items":[-1,"unexpected"]}}'
        third = self._build(original_dna, query_params=third_params)
        return [first, second, third]

    @staticmethod
    def _label(index: int) -> str:
        return ("numeric_boundary", "unicode_injection", "nested_type_confusion")[index]

    async def _health(
        self,
        target_url: str,
        client: httpx.AsyncClient,
        timeout: float | None = None,
    ) -> tuple[bool, str | None]:
        try:
            response = await asyncio.wait_for(
                client.get(f"{target_url.rstrip('/')}/health"),
                timeout=timeout or self.HEALTH_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                return False, f"health returned HTTP {response.status_code}"
            try:
                healthy = response.json().get("status") == "ok"
            except (TypeError, ValueError):
                healthy = False
            return (True, None) if healthy else (False, "health payload was not ok")
        except asyncio.TimeoutError:
            return False, "health check timed out"
        except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            return False, str(exc) or exc.__class__.__name__
        except Exception as exc:
            return False, str(exc) or exc.__class__.__name__

    @staticmethod
    def _response_status(result: dict[str, Any]) -> int:
        try:
            return int(result.get("status_code", 0))
        except (TypeError, ValueError):
            return 0

    def _survived(self, dna: AttackDNA, result: dict[str, Any], health_ok: bool) -> tuple[bool, str]:
        status_code = self._response_status(result)
        failure_kind = result.get("failure_kind")
        if not health_ok:
            return False, "Target health check failed after mutation"
        if status_code >= 500:
            if (
                dna.attack_type is AttackType.LATENCY
                and status_code == 504
                and "upstream_timeout_handled" in str(result.get("response_body", ""))
                and failure_kind in {None, "exception"}
            ):
                return True, "Controlled upstream timeout response with healthy target"
            return False, f"Unexpected server error HTTP {status_code}"
        if failure_kind in {"hang", "crash", "exception"} or status_code == 0:
            return False, result.get("error") or "Mutation raised an unhandled transport/application failure"
        if status_code in {200, 201, 202, 204}:
            return True, "Request handled within the execution budget"
        if status_code in {400, 408, 422}:
            return True, "Input rejected without an unhandled exception"
        return False, f"Unexpected HTTP status {status_code}"

    @staticmethod
    def _expected(dna: AttackDNA) -> str:
        if dna.attack_type is AttackType.LATENCY:
            return "2xx, validation 4xx, or controlled HTTP 504"
        return "2xx, HTTP 400/408, or HTTP 422"

    async def _replay(
        self,
        target_url: str,
        dna: AttackDNA,
        client: httpx.AsyncClient,
        timeout: float,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if dna.attack_type is AttackType.FUZZ:
            kwargs["request_timeout"] = min(1.5, timeout)
        elif dna.attack_type is AttackType.STORM:
            kwargs.update({"request_timeout": min(1.0, timeout), "sla_ms": 1500.0})
        elif dna.attack_type is AttackType.LATENCY:
            kwargs.update({"client_timeout": min(1.0, timeout), "sla_ms": 1500.0})
        return await asyncio.wait_for(
            replay(dna, target_url, client=client, **kwargs),
            timeout=timeout,
        )

    async def run_escalation(
        self,
        target_url: str,
        original_dna: AttackDNA,
        client: httpx.AsyncClient,
    ) -> dict:
        mutations = self.mutate_dna(original_dna)
        details: list[dict[str, Any]] = []
        started = time.perf_counter()

        for index, dna in enumerate(mutations):
            remaining = self.OVERALL_TIMEOUT_SECONDS - (time.perf_counter() - started)
            if remaining <= 0:
                break
            mutation_started = time.perf_counter()
            result: dict[str, Any] | None = None
            error: str | None = None
            try:
                result = await self._replay(
                    target_url,
                    dna,
                    client,
                    min(self.MUTATION_TIMEOUT_SECONDS, remaining),
                )
            except asyncio.TimeoutError:
                error = "mutation replay timed out"
            except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
                error = str(exc) or exc.__class__.__name__
            except Exception as exc:  # Keep one escaped application failure local to this mutation.
                error = str(exc) or exc.__class__.__name__

            health_remaining = self.OVERALL_TIMEOUT_SECONDS - (time.perf_counter() - started)
            if health_remaining <= 0:
                health_ok, health_error = False, "overall escalation budget exhausted before health check"
            else:
                try:
                    health_ok, health_error = await asyncio.wait_for(
                        self._health(
                            target_url,
                            client,
                            timeout=min(self.HEALTH_TIMEOUT_SECONDS, health_remaining),
                        ),
                        timeout=health_remaining,
                    )
                except asyncio.TimeoutError:
                    health_ok, health_error = False, "health check timed out"
            elapsed_ms = (time.perf_counter() - mutation_started) * 1000
            if result is None:
                survived = False
                reason = error or "Mutation could not be evaluated"
                status_code = 0
                response_error = error
            else:
                status_code = self._response_status(result)
                response_error = result.get("error")
                survived, reason = self._survived(dna, result, health_ok)
                if response_error and not survived:
                    reason = str(response_error)
            if not health_ok and health_error:
                reason = f"{reason}; {health_error}"
            details.append(
                {
                    "dna_id": dna.dna_id,
                    "sha256": dna.sha256,
                    "mutation": self._label(index),
                    "expected": self._expected(dna),
                    "status_code": status_code,
                    "elapsed_ms": round(elapsed_ms, 3),
                    "survived": bool(survived and health_ok),
                    "health_ok": health_ok,
                    "error": response_error or error or health_error,
                    "reason": reason,
                }
            )

            # Mutation A also checks the documented large-positive boundary.
            # It remains diagnostic evidence for the numeric mutation rather
            # than a fourth primary mutation.
            if index == 0 and original_dna.attack_type is AttackType.FUZZ:
                supplemental_payload = self._json_payload(dna)
                supplemental_payload["quantity"] = 10**18
                supplemental = self._build(dna, payload=supplemental_payload, raw_body=None)
                try:
                    supplemental_result = await self._replay(
                        target_url,
                        supplemental,
                        client,
                        min(1.0, max(0.1, self.OVERALL_TIMEOUT_SECONDS - (time.perf_counter() - started))),
                    )
                    supplemental_status = self._response_status(supplemental_result)
                    supplemental_ok = supplemental_status in {200, 201, 202, 204, 400, 408, 422}
                    details[-1]["supplementary"] = {
                        "dna_id": supplemental.dna_id,
                        "sha256": supplemental.sha256,
                        "quantity": 10**18,
                        "status_code": supplemental_status,
                        "survived": supplemental_ok,
                        "error": supplemental_result.get("error"),
                    }
                    if not supplemental_ok:
                        details[-1]["survived"] = False
                        details[-1]["reason"] = "Supplementary quantity=10**18 produced an unexpected result"
                except Exception as exc:
                    details[-1]["supplementary"] = {
                        "dna_id": supplemental.dna_id,
                        "sha256": supplemental.sha256,
                        "quantity": 10**18,
                        "status_code": 0,
                        "survived": False,
                        "error": str(exc) or exc.__class__.__name__,
                    }
                    details[-1]["survived"] = False
                    details[-1]["reason"] = "Supplementary quantity=10**18 could not be evaluated"

        all_survived = len(details) == len(mutations) == 3 and all(
            detail["survived"] for detail in details
        )
        return {
            "all_survived": all_survived,
            "mutations_tested": len(details),
            "details": details,
        }
