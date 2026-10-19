from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omniimmune.arbiter import Arbiter


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "target_app" / "app.py"


@pytest.mark.integration
def test_local_closed_loop_heals_all_vectors_and_reuses_memory(tmp_path: Path, monkeypatch) -> None:
    pristine = TARGET.read_text(encoding="utf-8")
    monkeypatch.setenv("OMNI_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("OMNI_REPORT_PATH", str(tmp_path / "INCIDENT_REPORT.md"))
    try:
        first = asyncio.run(
            Arbiter(
                "http://127.0.0.1:8000",
                "http://127.0.0.1:9000",
                mode="local",
                seed=1337,
                fresh=True,
                no_ui=True,
            ).run_closed_loop()
        )
        assert first.score == 100
        assert first.incidents_total == first.incidents_verified == 3
        assert (tmp_path / "INCIDENT_REPORT.md").exists()

        second = asyncio.run(
            Arbiter(
                "http://127.0.0.1:8000",
                "http://127.0.0.1:9000",
                mode="local",
                seed=1337,
                no_ui=True,
            ).run_closed_loop()
        )
        assert second.score == 100
        assert second.vaccinated_count == 3
    finally:
        TARGET.write_text(pristine, encoding="utf-8")
