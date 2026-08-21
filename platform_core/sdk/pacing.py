from __future__ import annotations

from collections import deque
from threading import RLock
from time import monotonic, sleep


class OrderEfficiencyError(RuntimeError):
    pass


class OrderPacingSDK:
    """进程级订单消息限速与订单效率保护。"""

    def __init__(
        self,
        *,
        max_messages_per_second: int = 20,
        max_order_efficiency_ratio: float = 20.0,
        minimum_messages_for_oer: int = 20,
    ) -> None:
        if max_messages_per_second <= 0 or max_order_efficiency_ratio <= 0:
            raise ValueError("pacing limits must be positive")
        self.max_messages_per_second = max_messages_per_second
        self.max_order_efficiency_ratio = max_order_efficiency_ratio
        self.minimum_messages_for_oer = minimum_messages_for_oer
        self._lock = RLock()
        self._message_times: deque[float] = deque()
        self._order_messages = 0
        self._executions = 0

    @property
    def order_efficiency_ratio(self) -> float:
        with self._lock:
            return self._order_messages / max(1, self._executions)

    def check_new_orders_allowed(self, *, messages: int = 1) -> None:
        if messages <= 0 or messages > self.max_messages_per_second:
            raise ValueError("invalid order message count")
        with self._lock:
            if (
                self._order_messages >= self.minimum_messages_for_oer
                and self.order_efficiency_ratio > self.max_order_efficiency_ratio
            ):
                raise OrderEfficiencyError(
                    f"order efficiency ratio {self.order_efficiency_ratio:.2f} exceeds "
                    f"{self.max_order_efficiency_ratio:.2f}"
                )

    def acquire(self, *, messages: int = 1) -> None:
        self._validate_message_count(messages)
        while True:
            wait_seconds = 0.0
            with self._lock:
                now = monotonic()
                while self._message_times and now - self._message_times[0] >= 1.0:
                    self._message_times.popleft()
                if len(self._message_times) + messages <= self.max_messages_per_second:
                    for _ in range(messages):
                        self._message_times.append(now)
                    self._order_messages += messages
                    return
                wait_seconds = max(0.001, 1.0 - (now - self._message_times[0]))
            sleep(wait_seconds)

    def record_execution(self) -> None:
        with self._lock:
            self._executions += 1

    def _validate_message_count(self, messages: int) -> None:
        if messages <= 0 or messages > self.max_messages_per_second:
            raise ValueError("invalid order message count")
