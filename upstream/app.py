import asyncio
from fastapi import FastAPI

app = FastAPI(title="Upstream Service", version="1.0.0")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/upstream/slow")
async def slow_endpoint(delay: float = 4):
    await asyncio.sleep(delay)
    return {"status": "upstream_ok"}
