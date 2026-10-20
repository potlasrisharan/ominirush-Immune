from __future__ import annotations

import asyncio
import json

import pytest

from omniimmune.events import EventBus, EventType
from omniimmune.ui.web import DashboardBroadcaster, DashboardStore


class ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_dashboard_broadcaster_updates_state_and_fans_out_events() -> None:
    bus = EventBus()
    store = DashboardStore()
    broadcaster = DashboardBroadcaster(bus, store)
    queue = broadcaster.subscribe()
    try:
        await bus.publish(
            EventType.PATCH_APPLIED,
            {"sig_id": "sig-demo", "diff": "-raise\n+return"},
        )
        payload = await asyncio.wait_for(queue.get(), timeout=1)
    finally:
        broadcaster.unsubscribe(queue)
        broadcaster.close()

    assert payload["type"] == EventType.PATCH_APPLIED
    assert payload["data"]["sig_id"] == "sig-demo"
    assert payload["state"]["status"] == "PATCH APPLIED"
    assert payload["state"]["diff"] == "-raise\n+return"


@pytest.mark.asyncio
async def test_dashboard_sse_stream_emits_named_json_event() -> None:
    bus = EventBus()
    broadcaster = DashboardBroadcaster(bus, DashboardStore())
    stream = broadcaster.stream(ConnectedRequest())
    pending = asyncio.create_task(stream.__anext__())
    try:
        await asyncio.sleep(0)
        await bus.publish(EventType.VERIFY_PASSED, {"sig_id": "sig-demo", "passed": True})
        chunk = await asyncio.wait_for(pending, timeout=1)
    finally:
        await stream.aclose()
        broadcaster.close()

    assert chunk.startswith("event: verify_passed\ndata: ")
    assert chunk.endswith("\n\n")
    payload = json.loads(chunk.split("data: ", 1)[1].strip())
    assert payload["type"] == EventType.VERIFY_PASSED
    assert payload["data"] == {"sig_id": "sig-demo", "passed": True}
