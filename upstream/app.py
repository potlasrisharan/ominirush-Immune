import asyncio
from fastapi import FastAPI, Query

app = FastAPI(title="Upstream External Service", version="1.0.0")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/upstream/slow")
async def slow_endpoint(delay: float = Query(default=3.0, ge=0.0, le=30.0)):
    # Simulates a slow third-party payment gateway or partner API
    await asyncio.sleep(delay)
    return {"status": "upstream_ok", "delay_simulated": delay}
