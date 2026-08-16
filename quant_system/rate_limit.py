"""Async sliding-window limits for exchange request weight and order counts."""

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Tuple


@dataclass
class RateLimitPolicy:
    request_weight_per_minute: int = 1200
    orders_per_10_seconds: int = 50
    orders_per_day: int = 160_000


class AsyncExchangeRateLimiter:
    def __init__(self, policy: RateLimitPolicy = None, clock=time.monotonic):
        self.policy = policy or RateLimitPolicy()
        self.clock = clock
        self.weight_events: Deque[Tuple[float, int]] = deque()
        self.short_order_events: Deque[Tuple[float, int]] = deque()
        self.daily_order_events: Deque[Tuple[float, int]] = deque()
        self.lock = asyncio.Lock()

    @staticmethod
    def _prune(events, now: float, window: float) -> None:
        while events and now - events[0][0] >= window:
            events.popleft()

    @staticmethod
    def _used(events) -> int:
        return sum(value for _, value in events)

    def _wait_for(self, events, now: float, window: float, limit: int, amount: int) -> float:
        if self._used(events) + amount <= limit:
            return 0.0
        running = self._used(events)
        for timestamp, value in events:
            running -= value
            if running + amount <= limit:
                return max(0.0, window - (now - timestamp))
        return window

    async def try_acquire(self, weight: int = 1, is_order: bool = False) -> bool:
        async with self.lock:
            now = self.clock()
            self._prune(self.weight_events, now, 60)
            self._prune(self.short_order_events, now, 10)
            self._prune(self.daily_order_events, now, 86_400)
            if self._used(self.weight_events) + weight > self.policy.request_weight_per_minute:
                return False
            if is_order and self._used(self.short_order_events) + 1 > self.policy.orders_per_10_seconds:
                return False
            if is_order and self._used(self.daily_order_events) + 1 > self.policy.orders_per_day:
                return False
            self.weight_events.append((now, weight))
            if is_order:
                self.short_order_events.append((now, 1))
                self.daily_order_events.append((now, 1))
            return True

    async def acquire(self, weight: int = 1, is_order: bool = False) -> None:
        if weight <= 0 or weight > self.policy.request_weight_per_minute:
            raise ValueError("request weight is outside limiter capacity")
        while True:
            async with self.lock:
                now = self.clock()
                self._prune(self.weight_events, now, 60)
                self._prune(self.short_order_events, now, 10)
                self._prune(self.daily_order_events, now, 86_400)
                waits = [
                    self._wait_for(
                        self.weight_events,
                        now,
                        60,
                        self.policy.request_weight_per_minute,
                        weight,
                    )
                ]
                if is_order:
                    waits.extend(
                        [
                            self._wait_for(
                                self.short_order_events,
                                now,
                                10,
                                self.policy.orders_per_10_seconds,
                                1,
                            ),
                            self._wait_for(
                                self.daily_order_events,
                                now,
                                86_400,
                                self.policy.orders_per_day,
                                1,
                            ),
                        ]
                    )
                wait = max(waits)
                if wait <= 0:
                    self.weight_events.append((now, weight))
                    if is_order:
                        self.short_order_events.append((now, 1))
                        self.daily_order_events.append((now, 1))
                    return
            await asyncio.sleep(min(wait, 60.0))

    async def snapshot(self) -> Dict[str, int]:
        async with self.lock:
            now = self.clock()
            self._prune(self.weight_events, now, 60)
            self._prune(self.short_order_events, now, 10)
            self._prune(self.daily_order_events, now, 86_400)
            return {
                "request_weight_1m": self._used(self.weight_events),
                "orders_10s": self._used(self.short_order_events),
                "orders_1d": self._used(self.daily_order_events),
            }

