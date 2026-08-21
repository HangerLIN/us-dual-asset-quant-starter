from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from platform_core.schemas import PlatformEvent


class EventBus(Protocol):
    def publish(self, event: PlatformEvent) -> None:
        ...


class InMemoryEventBus:
    def __init__(self) -> None:
        self.events: list[PlatformEvent] = []
        self._subscribers: list[Callable[[PlatformEvent], None]] = []

    def subscribe(self, callback: Callable[[PlatformEvent], None]) -> None:
        self._subscribers.append(callback)

    def publish(self, event: PlatformEvent) -> None:
        self.events.append(event)
        for callback in self._subscribers:
            callback(event)


class RedisStreamEventBus:
    """Redis Streams 发布器，用于在不改变事件契约的前提下拆分运行时服务。"""

    def __init__(self, redis_url: str, *, stream: str = "quant:events") -> None:
        from redis import Redis

        self._client = Redis.from_url(redis_url, decode_responses=True)
        self.stream = stream

    def publish(self, event: PlatformEvent) -> None:
        self._client.xadd(
            self.stream,
            {
                "event_type": event.event_type,
                "trace_id": event.trace_id,
                "payload": event.model_dump_json(),
            },
        )
