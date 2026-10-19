# OmniResilience Phase 2

Phase 2 implements the deterministic Red Swarm: payload fuzzing, serialized-load
stress, a latency/drop TCP proxy, and parallel attack orchestration. Phase 3 adds
the Blue Swarm: traceback/AST triage, a LibCST patch ladder, and DNA replay with
golden-path regression verification. Attack DNA is persisted so each failure can
be replayed exactly.

## Requirements

- Python 3.12.8 (the version is pinned in `.python-version`)
- A local checkout of this repository

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

All Python dependencies are pinned in `requirements.lock`. The project uses the
local `.venv`; no global package installation is required.

## Run locally

Start the bundled services in separate terminals:

```bash
UPSTREAM_URL=http://127.0.0.1:9001 .venv/bin/python -m uvicorn upstream.app:app \
  --host 127.0.0.1 --port 9001
UPSTREAM_URL=http://127.0.0.1:9001 .venv/bin/python -m uvicorn target_app.app:app \
  --host 127.0.0.1 --port 8000
```

The target health endpoint is `http://127.0.0.1:8000/health`.

## Test

Run the complete suite with the single project test command:

```bash
make test
```

The two explicit acceptance runs are:

```bash
.venv/bin/pytest -v tests/ -m "not integration"
.venv/bin/pytest -v tests/ -m integration
```

The integration test launches real Uvicorn subprocesses and uses only local
network sockets.

## Container build and run

```bash
```

The image starts the target service. Set `UPSTREAM_URL` to a reachable upstream
service when exercising the payment verification route.

## Phase 2 layout

- `omniimmune/chaos/common.py`: latency statistics, bug hints, and DNA replay
- `omniimmune/chaos/fuzzer.py`: `FuzzNet`
- `omniimmune/chaos/storm.py`: `StormLoad`
- `omniimmune/chaos/latency.py`: `LatencyProxy` and `LatencyDriftAgent`
- `omniimmune/chaos/runner.py`: `RedSwarmRunner`
- `omniimmune/healer/triage.py`: traceback and AST `TriageEngine`
- `omniimmune/healer/patcher.py`: LibCST `PatchLadder` with rollback
- `omniimmune/verifier/verifier.py`: `VerifierEngine` and golden regressions
- `artifacts/dna/`: runtime DNA artifacts, ignored by git
