from __future__ import annotations

import httpx
import pytest
from json import loads

from omniimmune.chaos.fuzzer import FuzzNet, discover_endpoints, synthesize_payloads


class _SpecResponse:
    status_code = 200

    def __init__(self, document):
        self._document = document

    def json(self):
        return self._document


class _SpecClient:
    def __init__(self, document):
        self.document = document
        self.urls: list[str] = []

    async def get(self, url):
        self.urls.append(url)
        return _SpecResponse(self.document)


@pytest.mark.asyncio
async def test_discover_endpoints_extracts_operations_parameters_and_refs():
    spec = {
        "components": {
            "schemas": {
                "Order": {
                    "type": "object",
                    "properties": {"quantity": {"type": "integer", "minimum": 1}},
                }
            }
        },
        "paths": {
            "/orders/{order_id}": {
                "parameters": [{"name": "order_id", "in": "path", "required": True}],
                "post": {
                    "parameters": [{"name": "verbose", "in": "query", "schema": {"type": "boolean"}}],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Order"}}},
                    },
                },
            }
        },
    }
    client = _SpecClient(spec)
    endpoints = await discover_endpoints("http://service/", client)

    assert client.urls == ["http://service/openapi.json"]
    assert endpoints[0]["path"] == "/orders/{order_id}"
    assert endpoints[0]["method"] == "POST"
    assert {parameter["name"] for parameter in endpoints[0]["parameters"]} == {"order_id", "verbose"}
    assert endpoints[0]["request_body_schema"]["properties"]["quantity"]["minimum"] == 1


def test_schema_payloads_are_deterministic_and_cover_boundaries_and_injection():
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 0, "maximum": 3},
            "name": {"type": "string", "maxLength": 4},
        },
    }
    first = synthesize_payloads(schema, seed=9)
    second = synthesize_payloads(schema, seed=9)

    assert first == second
    assert any(payload.get("count") == -1 for payload in first)
    assert any(payload.get("count") == 4 for payload in first)
    assert any("OR 1=1" in payload.get("name", "") for payload in first)
    assert any(len(payload.get("name", "")) == 4 for payload in first)


@pytest.mark.asyncio
async def test_fuzzer_uses_discovered_schema_operation_and_falls_back_on_failure():
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "paths": {
                        "/v2/widgets": {
                            "post": {
                                "requestBody": {
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "type": "object",
                                                "properties": {"size": {"type": "integer"}},
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                },
            )
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dna, result = await FuzzNet(seed=4).run("http://service", client=client)

    assert dna.target_endpoint == "/v2/widgets"
    assert dna.http_method == "POST"
    assert calls[1].url.path == "/v2/widgets"
    assert loads(calls[1].content) == {"size": -5}
    assert result["is_failure"] is False


@pytest.mark.asyncio
async def test_discovery_failure_returns_empty_list():
    class BrokenClient:
        async def get(self, url):
            raise httpx.ConnectError("offline")

    assert await discover_endpoints("http://offline", BrokenClient()) == []
