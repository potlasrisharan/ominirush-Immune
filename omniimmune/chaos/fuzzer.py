from __future__ import annotations

import asyncio
import copy
import math
import random
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from omniimmune.models import AttackDNA, AttackType

from .common import response_body


_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


class _StableNaN(float):
    """JSON-hostile NaN whose equality remains deterministic in test vectors."""

    def __new__(cls) -> "_StableNaN":
        return float.__new__(cls, float("nan"))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, float) and math.isnan(other)

    def __hash__(self) -> int:
        return hash("stable-nan")


def _local_ref(spec: Mapping[str, Any], value: Any) -> Any:
    """Resolve the local JSON references used by normal OpenAPI documents."""
    if not isinstance(value, Mapping) or not isinstance(value.get("$ref"), str):
        return value
    reference = value["$ref"]
    if not reference.startswith("#/"):
        return value
    resolved: Any = spec
    try:
        for part in reference[2:].split("/"):
            resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
    except (KeyError, TypeError):
        return value
    return resolved


def _resolved_schema(spec: Mapping[str, Any], schema: Any) -> dict[str, Any]:
    """Return a detached, locally resolved schema without mutating the spec."""
    current = schema
    seen: set[str] = set()
    while isinstance(current, Mapping) and isinstance(current.get("$ref"), str):
        ref = current["$ref"]
        if ref in seen:
            break
        seen.add(ref)
        current = _local_ref(spec, current)
    if not isinstance(current, Mapping):
        return {}

    def expand(value: Any, refs: set[str]) -> Any:
        if isinstance(value, Mapping):
            if isinstance(value.get("$ref"), str) and value["$ref"] not in refs:
                ref = value["$ref"]
                target = _local_ref(spec, value)
                if target is not value:
                    return expand(target, refs | {ref})
            return {key: expand(item, refs) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item, refs) for item in value]
        return value

    return expand(current, seen)


async def discover_endpoints(
    target_url: str, client: httpx.AsyncClient | None = None
) -> list[dict[str, Any]]:
    """Fetch and normalize the target's OpenAPI operations.

    Discovery is deliberately best-effort: a missing, malformed, or unavailable
    ``/openapi.json`` returns an empty list so callers can retain the original
    deterministic order-fuzzing behavior.  Each returned item is one operation,
    with both the raw OpenAPI request body and its resolved JSON schema exposed.
    """
    owned_client = client is None
    active_client = client if client is not None else httpx.AsyncClient()
    try:
        response = await active_client.get(f"{target_url.rstrip('/')}/openapi.json")
        if int(getattr(response, "status_code", 500)) >= 400:
            return []
        document = response.json()
        if not isinstance(document, Mapping) or not isinstance(document.get("paths"), Mapping):
            return []

        endpoints: list[dict[str, Any]] = []
        for path, path_item in document["paths"].items():
            if not isinstance(path, str) or not isinstance(path_item, Mapping):
                continue
            path_parameters = path_item.get("parameters", [])
            if not isinstance(path_parameters, list):
                path_parameters = []
            for method, operation in path_item.items():
                if method.lower() not in _HTTP_METHODS or not isinstance(operation, Mapping):
                    continue
                parameters = list(path_parameters)
                operation_parameters = operation.get("parameters", [])
                if isinstance(operation_parameters, list):
                    # Operation parameters override path-level parameters by name/location.
                    overridden = {
                        (item.get("name"), item.get("in"))
                        for item in operation_parameters
                        if isinstance(item, Mapping)
                    }
                    parameters = [
                        item
                        for item in parameters
                        if not isinstance(item, Mapping)
                        or (item.get("name"), item.get("in")) not in overridden
                    ] + list(operation_parameters)

                request_body = operation.get("requestBody")
                request_body_schema: dict[str, Any] = {}
                if isinstance(request_body, Mapping):
                    body = _local_ref(document, request_body)
                    content = body.get("content", {}) if isinstance(body, Mapping) else {}
                    if isinstance(content, Mapping):
                        media = content.get("application/json")
                        if not isinstance(media, Mapping) and content:
                            media = next(iter(content.values()))
                        if isinstance(media, Mapping):
                            request_body_schema = _resolved_schema(document, media.get("schema", {}))

                # OpenAPI 2 describes a JSON body as a parameter instead of requestBody.
                if not request_body_schema:
                    for parameter in parameters:
                        if not isinstance(parameter, Mapping) or parameter.get("in") != "body":
                            continue
                        request_body_schema = _resolved_schema(document, parameter.get("schema", {}))
                        break

                normalized_parameters = [
                    copy.deepcopy(_local_ref(document, parameter))
                    if isinstance(parameter, Mapping)
                    else parameter
                    for parameter in parameters
                ]
                endpoints.append(
                    {
                        "path": path,
                        "method": method.upper(),
                        "parameters": normalized_parameters,
                        "requestBody": copy.deepcopy(request_body) if isinstance(request_body, Mapping) else None,
                        "request_body": copy.deepcopy(request_body) if isinstance(request_body, Mapping) else None,
                        "request_body_schema": request_body_schema,
                        "schema": request_body_schema,
                    }
                )
        return endpoints
    except Exception:
        return []
    finally:
        if owned_client:
            await active_client.aclose()


def _unique(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _schema_values(schema: Mapping[str, Any], seed: int = 1337) -> list[Any]:
    """Build stable boundary and injection candidates for one JSON schema node."""
    schema_type = schema.get("type")
    if not schema_type and "properties" in schema:
        schema_type = "object"
    if schema_type == "integer":
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        values: list[Any] = [-5, -1, 0, 1, 9223372036854775807, _StableNaN()]
        if isinstance(minimum, int):
            values = [minimum - 1, minimum, *values]
        if isinstance(maximum, int):
            values.extend([maximum, maximum + 1])
        return _unique(values)
    if schema_type == "number":
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        values = [-1, 0, 1, 1e308]
        if isinstance(minimum, (int, float)):
            values = [minimum - 1, minimum, *values]
        if isinstance(maximum, (int, float)):
            values.extend([maximum, maximum + 1])
        return _unique(values)
    if schema_type == "string":
        max_length = schema.get("maxLength")
        long_value = "A" * (min(int(max_length), 5000) if isinstance(max_length, int) else 2048)
        return _unique([
            "fuzz-string",
            "",
            "' OR 1=1; DROP TABLE orders;--",
            "$(id); `whoami` && cat /etc/passwd",
            "<script>alert(1)</script>",
            "\u202e" + ("界" * 2048),
            long_value,
        ])
    if schema_type == "boolean":
        return [False, True]
    if schema_type == "array":
        item_schema = schema.get("items", {})
        item_values = _schema_values(item_schema, seed) if isinstance(item_schema, Mapping) else [None]
        return [[], [item_values[0]], [item_values[-1]]]
    if schema_type == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            return [{}]
        base: dict[str, Any] = {}
        candidates: list[dict[str, Any]] = []
        for name, property_schema in properties.items():
            if not isinstance(property_schema, Mapping):
                continue
            values = _schema_values(property_schema, seed)
            if values:
                # Keep the historical order attack as the first discovered payload.
                if name == "item_id" and property_schema.get("type") == "string":
                    base[name] = "item-neg"
                elif name == "quantity" and property_schema.get("type") == "integer":
                    base[name] = -5
                else:
                    base[name] = values[0]
        candidates.append(base)
        # Required-field omissions, deeply nested objects, and type confusion
        # exercise validators beyond the happy-path shape.
        candidates.append({})
        candidates.append({"nested": {"nested": {"nested": {"value": "overflow"}}}})
        for name, property_schema in properties.items():
            if not isinstance(property_schema, Mapping):
                continue
            for value in _schema_values(property_schema, seed):
                mutated = dict(base)
                mutated[name] = value
                candidates.append(mutated)
            for wrong_type in (None, [], {}, "type-confusion"):
                mutated = dict(base)
                mutated[name] = wrong_type
                candidates.append(mutated)
        return _unique(candidates)
    return [None]


def synthesize_payloads(schema: Mapping[str, Any], seed: int = 1337) -> list[Any]:
    """Public deterministic schema-aware payload synthesis helper."""
    return _schema_values(schema, seed)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN"
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


class FuzzNet:
    def __init__(self, seed: int = 1337) -> None:
        self.seed = seed

    def generate_payloads(self, endpoint: Mapping[str, Any] | None = None) -> list[Any]:
        if endpoint is not None:
            schema = endpoint.get("request_body_schema") or endpoint.get("schema")
            if isinstance(schema, Mapping) and schema:
                return synthesize_payloads(schema, self.seed)
            parameters = endpoint.get("parameters", [])
            parameter_values: dict[str, list[Any]] = {}
            if isinstance(parameters, list):
                for parameter in parameters:
                    if not isinstance(parameter, Mapping) or parameter.get("in") not in {"query", "path"}:
                        continue
                    name = parameter.get("name")
                    parameter_schema = parameter.get("schema", parameter)
                    if name and isinstance(parameter_schema, Mapping):
                        parameter_values[str(name)] = _schema_values(parameter_schema, self.seed)
            if parameter_values:
                base = {name: values[0] for name, values in parameter_values.items()}
                candidates: list[dict[str, Any]] = [base]
                for name, values in parameter_values.items():
                    for value in values:
                        mutated = dict(base)
                        mutated[name] = value
                        candidates.append(mutated)
                return _unique(candidates)
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

    def build_dna(
        self,
        payload_idx: int = 0,
        endpoint: Mapping[str, Any] | None = None,
        payloads: list[Any] | None = None,
    ) -> AttackDNA:
        candidates = payloads if payloads is not None else self.generate_payloads(endpoint)
        payload = candidates[payload_idx]
        target_endpoint = str(endpoint.get("path", "/api/v1/order")) if endpoint else "/api/v1/order"
        http_method = str(endpoint.get("method", "POST")).upper() if endpoint else "POST"
        query_params: dict[str, str] = {}
        body_payload: dict | list | str | None = None
        has_body = bool(endpoint and (endpoint.get("request_body_schema") or endpoint.get("schema") or endpoint.get("requestBody")))
        if endpoint and not has_body:
            for parameter in endpoint.get("parameters", []):
                if not isinstance(parameter, Mapping) or parameter.get("in") not in {"query", "path"}:
                    continue
                name = parameter.get("name")
                if name:
                    value = str(payload) if not isinstance(payload, Mapping) else payload.get(name, "")
                    if parameter.get("in") == "path":
                        target_endpoint = target_endpoint.replace(
                            "{" + str(name) + "}", quote(str(value), safe="")
                        )
                    else:
                        query_params[str(name)] = str(value)
        else:
            body_payload = None if isinstance(payload, str) else payload
        return AttackDNA(
            attack_type=AttackType.FUZZ,
            target_endpoint=target_endpoint,
            http_method=http_method,
            payload=body_payload,
            raw_body=payload if isinstance(payload, str) else None,
            query_params=query_params,
            headers={"Content-Type": "application/json"} if body_payload is not None or isinstance(payload, str) else {},
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
                request_kwargs["json"] = _json_safe(dna.payload)
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
        self,
        target_url: str,
        client: httpx.AsyncClient | None = None,
        endpoints: list[dict[str, Any]] | None = None,
    ) -> tuple[AttackDNA, dict]:
        if endpoints is None:
            endpoints = await discover_endpoints(target_url, client)
        # Prefer request-body operations while retaining the OpenAPI document's
        # order. This keeps deterministic legacy targets first and still lets
        # newly discovered endpoints participate without lexical path surprises.
        body_endpoints = [
            endpoint
            for endpoint in endpoints
            if endpoint.get("request_body_schema") or endpoint.get("schema")
        ]
        ordered_endpoints = body_endpoints or list(endpoints)
        if ordered_endpoints:
            last: tuple[AttackDNA, dict] | None = None
            for endpoint in ordered_endpoints:
                payloads = self.generate_payloads(endpoint)
                for payload_idx in range(len(payloads)):
                    dna = self.build_dna(payload_idx, endpoint=endpoint, payloads=payloads)
                    result = await self.execute(target_url, dna, client=client)
                    result["swept"] = payload_idx + 1
                    last = (dna, result)
                    if result["is_failure"]:
                        return last
            assert last is not None
            return last

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
