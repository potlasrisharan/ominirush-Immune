from __future__ import annotations

import json

import pytest

from omniimmune.events import EventBus, EventType


@pytest.mark.asyncio
async def test_event_bus_keeps_history_and_notifies_sync_and_async_subscribers() -> None:
    bus = EventBus()
    received: list[tuple[str, str]] = []

    def synchronous(event) -> None:
        received.append(("sync", event.type))

    async def asynchronous(event) -> None:
        received.append(("async", event.type))

    bus.subscribe(EventType.PATCH_APPLIED, synchronous)
    bus.subscribe_all(asynchronous)

    event = await bus.publish(
        EventType.PATCH_APPLIED,
        {"sig_id": "sig-demo", "strategy": "guard", "verified": True},
    )

    assert event is bus.history[0]
    assert event.type == EventType.PATCH_APPLIED
    assert received == [("sync", EventType.PATCH_APPLIED), ("async", EventType.PATCH_APPLIED)]

    # Event payloads are the wire contract used by streaming consumers.
    encoded = json.dumps(event.model_dump(mode="json"), sort_keys=True)
    assert '"type": "patch_applied"' in encoded
    assert '"sig_id": "sig-demo"' in encoded


@pytest.mark.asyncio
async def test_event_bus_unsubscribe_stops_future_notifications() -> None:
    bus = EventBus()
    received: list[str] = []

    def callback(event) -> None:
        received.append(event.type)

    bus.subscribe_all(callback)
    bus.unsubscribe_all(callback)
    await bus.publish(EventType.RUN_COMPLETE, {"score": 100})

    assert received == []
    assert [event.type for event in bus.history] == [EventType.RUN_COMPLETE]
