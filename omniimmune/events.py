from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class EventType:
    ATTACK_STARTED = "attack_started"
    FAILURE_DETECTED = "failure_detected"
    TRIAGE_DONE = "triage_done"
    PATCH_PROPOSED = "patch_proposed"
    PATCH_APPLIED = "patch_applied"
    VERIFY_STARTED = "verify_started"
    VERIFY_PASSED = "verify_passed"
    VERIFY_FAILED = "verify_failed"
    ROLLED_BACK = "rolled_back"
    MEMORY_HIT = "memory_hit"
    RUN_COMPLETE = "run_complete"


class Event(BaseModel):
    type: str
    data: dict
    ts: float = Field(default_factory=time.time)


Callback = Callable[[Event], object | Awaitable[object]]


class EventBus:
    def __init__(self) -> None:
        self._subscriptions: list[tuple[str, Callback]] = []
        self.history: list[Event] = []

    def subscribe(self, event_type: str, callback: Callback) -> None:
        self._subscriptions.append((event_type, callback))

    def unsubscribe(self, event_type: str, callback: Callback) -> None:
        self._subscriptions = [
            (registered_type, registered_callback)
            for registered_type, registered_callback in self._subscriptions
            if not (registered_type == event_type and registered_callback == callback)
        ]

    async def publish(self, event_type: str, data: dict) -> Event:
        event = Event(type=event_type, data=data)
        self.history.append(event)
        for registered_type, callback in tuple(self._subscriptions):
            if registered_type not in (event_type, "*"):
                continue
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Event subscriber failed for %s", event_type)
        return event
