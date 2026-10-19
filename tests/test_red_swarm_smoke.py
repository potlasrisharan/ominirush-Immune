from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from omniimmune.chaos.runner import RedSwarmRunner
from omniimmune.events import EventBus
from omniimmune.models import AttackType


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_for_health(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get(url)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise AssertionError(f"service did not become healthy: {url}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_red_swarm_against_real_processes(tmp_path: Path) -> None:
    upstream_port = free_port()
    target_port = free_port()
    stderr_path = tmp_path / "target.stderr"
    upstream = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "upstream.app:app", "--host", "127.0.0.1", "--port", str(upstream_port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    environment = os.environ.copy()
    environment["UPSTREAM_URL"] = f"http://127.0.0.1:{upstream_port}"
    with stderr_path.open("w+") as stderr:
        target = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "target_app.app:app", "--host", "127.0.0.1", "--port", str(target_port)],
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            env=environment,
        )
        try:
            await wait_for_health(f"http://127.0.0.1:{upstream_port}/health")
            await wait_for_health(f"http://127.0.0.1:{target_port}/health")
            async with httpx.AsyncClient() as client:
                results = await RedSwarmRunner(
                    f"http://127.0.0.1:{target_port}",
                    EventBus(),
                    artifacts_dir=str(tmp_path / "artifacts" / "dna"),
                    storm_requests=20,
                    latency_delay=4.0,
                ).run_all_parallel(client=client)
            print("vector failure_kind status_code elapsed_ms")
            for (dna, result) in results:
                print(dna.attack_type.value, result["failure_kind"], result["status_code"], f"{result['elapsed_ms']:.1f}")
            assert all(result["is_failure"] for _, result in results)
            stderr.flush()
            target_stderr = stderr_path.read_text(encoding="utf-8")
            assert "CRITICAL_ORDER_PANIC" in target_stderr
            assert "Traceback" in target_stderr
        finally:
            target.terminate()
            try:
                target.wait(timeout=5)
            except subprocess.TimeoutExpired:
                target.kill()
                target.wait(timeout=5)
            upstream.terminate()
            try:
                upstream.wait(timeout=5)
            except subprocess.TimeoutExpired:
                upstream.kill()
                upstream.wait(timeout=5)
