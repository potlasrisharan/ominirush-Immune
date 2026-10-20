from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import DEFAULT_DASHBOARD_PORT, parse_args
from omniimmune.ui.web import create_dashboard_app


ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_serve_dashboard_port_is_optional_and_validated() -> None:
    assert parse_args(["--serve-dashboard"]).serve_dashboard == DEFAULT_DASHBOARD_PORT
    assert parse_args(["--serve-dashboard", "9123"]).serve_dashboard == 9123
    with pytest.raises(SystemExit):
        parse_args(["--serve-dashboard", "0"])


def test_serve_dashboard_starts_without_running_chaos_loop() -> None:
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "main.py", "--serve-dashboard", str(port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        response = None
        while time.monotonic() < deadline:
            try:
                response = urllib.request.urlopen(f"http://127.0.0.1:{port}/dashboard", timeout=1)
                break
            except (urllib.error.URLError, TimeoutError):
                if process.poll() is not None:
                    break
                time.sleep(0.05)
        assert response is not None, process.stderr.read() if process.stderr else "dashboard did not start"
        body = response.read().decode("utf-8")
        assert "OmniImmune · Mission Control" in body
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_dashboard_renders_features_and_accepts_summary_state() -> None:
    client = TestClient(create_dashboard_app())
    initial = client.get("/dashboard")
    assert initial.status_code == 200
    assert "100/100" in initial.text
    assert "Red Swarm" in initial.text
    assert "Target App" in initial.text
    assert "Blue Swarm" in initial.text
    assert "Cryptographic Vault" in initial.text
    assert "LATENCY CONTAINMENT" in initial.text
    assert "LIBCST SYNTAX DIFF" in initial.text
    assert "heal/sig-order-panic-01" in initial.text

    updated = client.put(
        "/dashboard/state",
        json={
            "score": 100,
            "incidents_verified": 1,
            "incidents": [
                {
                    "branch": "heal/sig-demo123",
                    "branch_status": "CREATED",
                    "before": {"p99_ms": 420.0},
                    "after": {"p99_ms": 12.0},
                }
            ],
            "patch_diffs": ["@@ -1 +1 @@\n-raise\n+return"],
        },
    )
    assert updated.status_code == 200
    state = client.get("/dashboard/state").json()
    assert state["resilience_score"] == 100
    assert state["branches"][0]["name"] == "heal/sig-demo123"
    assert state["latency"] == {
        "before_ms": 420.0,
        "after_ms": 12.0,
        "before_label": "before heal",
        "after_label": "after heal",
    }
    assert "heal/sig-demo123" in client.get("/dashboard").text
