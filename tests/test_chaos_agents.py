from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from omniimmune.chaos.common import compute_latency_stats, replay
from omniimmune.chaos.fuzzer import FuzzNet
from omniimmune.chaos.latency import LatencyDriftAgent, LatencyProxy
from omniimmune.chaos.runner import RedSwarmRunner
from omniimmune.chaos.storm import StormLoad
from omniimmune.events import EventBus, EventType
from omniimmune.models import AttackDNA, AttackType
from target_app import app as target_app


def target_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=target_app.app, raise_app_exceptions=False),
        base_url="http://test",
    )


def test_latency_stats_use_nearest_rank_and_empty_zeroes() -> None:
    stats = compute_latency_stats(range(100, 1001, 100), [200] * 10)
    assert (stats.p50_ms, stats.p95_ms, stats.p99_ms) == (500, 1000, 1000)
    stats = compute_latency_stats([1] * 10, [200] * 8 + [500, 0])
    assert stats.error_rate == 0.2
    assert stats.survival_rate == 0.8
    empty = compute_latency_stats([], [])
    assert empty.model_dump() == {
        "p50_ms": 0.0,
        "p95_ms": 0.0,
        "p99_ms": 0.0,
        "error_rate": 0.0,
        "survival_rate": 0.0,
        "peak_mem_mb": 0.0,
    }


def test_fuzzer_payloads_and_dna_are_deterministic() -> None:
    first = FuzzNet(seed=1337)
    second = FuzzNet(seed=1337)
    different = FuzzNet(seed=1338)
    assert first.generate_payloads() == second.generate_payloads()
    assert [first.build_dna(i).sha256 for i in range(8)] == [
        second.build_dna(i).sha256 for i in range(8)
    ]
    assert first.generate_payloads()[5:] != different.generate_payloads()[5:]
    assert first.build_dna(3).raw_body is not None
    assert first.build_dna(3).payload is None
    assert first.build_dna(3).headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_fuzzer_against_target_and_first_failure() -> None:
    agent = FuzzNet()
    async with target_client() as client:
        negative = await agent.execute("http://test", agent.build_dna(0), client=client)
        invalid = await agent.execute("http://test", agent.build_dna(2), client=client)
        malformed = await agent.execute("http://test", agent.build_dna(3), client=client)
        overflow = await agent.execute("http://test", agent.build_dna(4), client=client)
        dna, result = await agent.run("http://test", client=client)
    assert negative["status_code"] == 500
    assert negative["is_failure"] is True
    assert negative["failure_kind"] == "exception"
    assert invalid["status_code"] == malformed["status_code"] == 422
    assert not invalid["is_failure"] and not malformed["is_failure"]
    assert overflow["status_code"] == 200
    assert dna == agent.build_dna(0)
    assert result["is_failure"] is True and result["swept"] == 1


def test_storm_dna_knobs() -> None:
    dna = StormLoad(default_concurrency=50).build_dna(concurrency=3, request_count=7)
    assert dna.concurrency == 3
    assert dna.request_count == 7
    assert dna.attack_type is AttackType.STORM


def test_chaos_knobs_reject_non_executing_or_invalid_faults() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        StormLoad().build_dna(concurrency=0)
    with pytest.raises(ValueError, match="positive integer"):
        StormLoad().build_dna(request_count=0)
    with pytest.raises(ValueError, match="non-negative"):
        LatencyProxy("localhost", 9000, delay_seconds=-1)
    with pytest.raises(ValueError, match="between 0 and 1"):
        LatencyProxy("localhost", 9000, drop_rate=1.1)


@pytest.mark.asyncio
async def test_storm_against_target_populates_stats() -> None:
    agent = StormLoad(default_concurrency=10)
    dna = agent.build_dna(concurrency=10, request_count=10)
    started = time.perf_counter()
    async with target_client() as client:
        result = await agent.execute("http://test", dna, client=client)
    elapsed = time.perf_counter() - started
    assert elapsed >= 0.8 * 10 * 0.12
    assert result["stats"].p99_ms > 0
    assert result["status_codes"] == {"200": 10}


@pytest.mark.asyncio
async def test_storm_sla_and_error_failure_logic() -> None:
    lock = asyncio.Lock()

    async def serialized(request: httpx.Request) -> httpx.Response:
        async with lock:
            await asyncio.sleep(0.05)
        return httpx.Response(200, json={"ok": True})

    agent = StormLoad()
    dna = agent.build_dna(concurrency=10, request_count=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(serialized)) as client:
        slow = await agent.execute("http://mock", dna, client=client, sla_ms=200)
        healthy = await agent.execute("http://mock", dna, client=client, sla_ms=10000)
    assert slow["is_failure"] is True and slow["failure_kind"] == "hang"
    assert healthy["is_failure"] is False

    count = 0

    async def half_errors(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(500 if count % 2 else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(half_errors)) as client:
        errors = await agent.execute("http://mock", dna, client=client, sla_ms=10000)
    assert errors["is_failure"] is True and errors["failure_kind"] == "exception"


async def _echo_server() -> tuple[asyncio.Server, int]:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1])


@pytest.mark.asyncio
async def test_latency_proxy_delay_drop_seed_and_shutdown() -> None:
    upstream, upstream_port = await _echo_server()
    proxy = LatencyProxy(
        upstream_host="127.0.0.1", upstream_port=upstream_port, delay_seconds=0, seed=12
    )
    same_seed = LatencyProxy(
        upstream_host="127.0.0.1", upstream_port=upstream_port, seed=12, drop_rate=0.5
    )
    try:
        await proxy.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        started = time.perf_counter()
        writer.write(b"fast")
        await writer.drain()
        assert await reader.readexactly(4) == b"fast"
        assert time.perf_counter() - started < 0.1
        writer.close()
        await writer.wait_closed()

        proxy.set_chaos(0.15, 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        started = time.perf_counter()
        writer.write(b"slow")
        await writer.drain()
        assert await reader.readexactly(4) == b"slow"
        assert time.perf_counter() - started >= 0.14
        writer.close()
        await writer.wait_closed()

        same_seed_again = LatencyProxy(
            upstream_host="127.0.0.1", upstream_port=upstream_port, seed=12, drop_rate=0.5
        )
        assert [same_seed._should_drop() for _ in range(8)] == [
            same_seed_again._should_drop() for _ in range(8)
        ]
    finally:
        await proxy.stop()
        await same_seed.stop()
        upstream.close()
        await upstream.wait_closed()

    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", proxy.port)


@pytest.mark.asyncio
async def test_latency_proxy_drop_and_stop_connected_client() -> None:
    upstream, upstream_port = await _echo_server()
    proxy = LatencyProxy(
        upstream_host="127.0.0.1", upstream_port=upstream_port, drop_rate=1.0
    )
    await proxy.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    writer.write(b"dropped")
    await writer.drain()
    try:
        assert await reader.read() == b""
    except ConnectionResetError:
        pass
    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionResetError:
        pass
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    await asyncio.wait_for(proxy.stop(), timeout=3)
    writer.close()
    await writer.wait_closed()
    upstream.close()
    await upstream.wait_closed()


class _FakeResponse:
    status_code = 200
    text = '{"status":"upstream_ok"}'

    def json(self):
        return {"status": "upstream_ok"}


@pytest.mark.asyncio
async def test_latency_agent_timeout_and_fast_fake(monkeypatch) -> None:
    class HangingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            await asyncio.sleep(30)

    monkeypatch.setattr(
        target_app,
        "httpx",
        SimpleNamespace(AsyncClient=HangingClient),
    )
    dna = LatencyDriftAgent().build_dna(4)
    started = time.perf_counter()
    async with target_client() as client:
        timed_out = await LatencyDriftAgent().execute(
            "http://test", dna, client=client, client_timeout=0.3
        )
    assert time.perf_counter() - started < 2
    assert timed_out["status_code"] == 0
    assert timed_out["failure_kind"] == "hang"

    class FastClient(HangingClient):
        async def get(self, url, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr(target_app, "httpx", SimpleNamespace(AsyncClient=FastClient))
    async with target_client() as client:
        fast = await LatencyDriftAgent().execute("http://test", dna, client=client)
    assert fast["status_code"] == 200 and fast["is_failure"] is False


@pytest.mark.asyncio
async def test_runner_artifacts_events_and_replay(tmp_path, monkeypatch) -> None:
    class HangingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            await asyncio.sleep(30)

    monkeypatch.setattr(target_app, "httpx", SimpleNamespace(AsyncClient=HangingClient))
    bus = EventBus()
    runner = RedSwarmRunner(
        "http://test",
        bus,
        artifacts_dir=str(tmp_path / "artifacts" / "dna"),
        storm_requests=8,
        client_timeout=0.3,
    )
    async with target_client() as client:
        fuzz_dna, fuzz_result = await runner.run_vector(AttackType.FUZZ, client=client)
        loaded = AttackDNA.from_file(tmp_path / "artifacts" / "dna" / f"{fuzz_dna.dna_id}.json")
        fuzz_replay = await replay(loaded, "http://test", client=client)
    assert loaded.verify_integrity()
    assert [event.type for event in bus.history] == [
        EventType.ATTACK_STARTED,
        EventType.FAILURE_DETECTED,
        EventType.ATTACK_FINISHED,
    ]
    assert bus.history[1].data["dna_id"] == fuzz_dna.dna_id
    assert bus.history[1].data["dna_sha256"] == fuzz_dna.sha256
    assert bus.history[2].data["dna_sha256"] == fuzz_dna.sha256
    assert fuzz_replay["status_code"] == fuzz_result["status_code"]
    assert fuzz_replay["is_failure"] == fuzz_result["is_failure"]

    bus = EventBus()
    runner.event_bus = bus
    async with target_client() as client:
        results = await runner.run_all_parallel(client=client)
        latency_dna, latency_result = results[2]
        latency_replay = await replay(latency_dna, "http://test", client=client, client_timeout=0.3)
    assert [dna.attack_type for dna, _ in results] == [
        AttackType.FUZZ,
        AttackType.STORM,
        AttackType.LATENCY,
    ]
    assert len(list((tmp_path / "artifacts" / "dna").glob("*.json"))) == 3
    assert results[0][1]["is_failure"] is True
    assert latency_result["is_failure"] is True
    assert latency_replay["status_code"] == latency_result["status_code"]
    assert latency_replay["is_failure"] == latency_result["is_failure"]
