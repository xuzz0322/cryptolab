"""Typed events and an in-process asynchronous event bus."""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional
from uuid import uuid4

from .exchange import AccountSnapshot
from .models import Bar
from .order_models import Fill, Order, Side


class EventKind(str, Enum):
    MARKET = "MARKET"
    SIGNAL = "SIGNAL"
    ORDER_INTENT = "ORDER_INTENT"
    RISK_DECISION = "RISK_DECISION"
    ORDER_UPDATE = "ORDER_UPDATE"
    FILL = "FILL"
    RECONCILIATION = "RECONCILIATION"
    CONTROL = "CONTROL"


def _event_id() -> str:
    return uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MarketEvent:
    symbol: str
    bar: Bar
    event_id: str = field(default_factory=_event_id)
    created_at: datetime = field(default_factory=_now)
    kind: EventKind = field(init=False, default=EventKind.MARKET)


@dataclass(frozen=True)
class SignalEvent:
    symbol: str
    target_weight: float
    strategy_name: str
    strategy_version: str
    reason: str
    trading_date: date
    event_id: str = field(default_factory=_event_id)
    causation_id: Optional[str] = None
    created_at: datetime = field(default_factory=_now)
    kind: EventKind = field(init=False, default=EventKind.SIGNAL)


@dataclass(frozen=True)
class OrderIntentEvent:
    symbol: str
    side: Side
    quantity: float
    reference_price: float
    strategy_name: str
    strategy_version: str
    reason: str
    trading_date: date
    intent_id: str = field(default_factory=_event_id)
    event_id: str = field(default_factory=_event_id)
    causation_id: Optional[str] = None
    created_at: datetime = field(default_factory=_now)
    kind: EventKind = field(init=False, default=EventKind.ORDER_INTENT)


@dataclass(frozen=True)
class RiskDecisionEvent:
    intent: OrderIntentEvent
    approved: bool
    reasons: List[str]
    approved_quantity: float
    event_id: str = field(default_factory=_event_id)
    created_at: datetime = field(default_factory=_now)
    kind: EventKind = field(init=False, default=EventKind.RISK_DECISION)


@dataclass(frozen=True)
class OrderUpdateEvent:
    order: Order
    source: str
    event_id: str = field(default_factory=_event_id)
    causation_id: Optional[str] = None
    created_at: datetime = field(default_factory=_now)
    kind: EventKind = field(init=False, default=EventKind.ORDER_UPDATE)


@dataclass(frozen=True)
class FillEvent:
    fill: Fill
    source: str
    event_id: str = field(default_factory=_event_id)
    causation_id: Optional[str] = None
    created_at: datetime = field(default_factory=_now)
    kind: EventKind = field(init=False, default=EventKind.FILL)


@dataclass(frozen=True)
class ReconciliationEvent:
    snapshot: AccountSnapshot
    discrepancies: List[str]
    event_id: str = field(default_factory=_event_id)
    created_at: datetime = field(default_factory=_now)
    kind: EventKind = field(init=False, default=EventKind.RECONCILIATION)


@dataclass(frozen=True)
class ControlEvent:
    command: str
    reason: str
    event_id: str = field(default_factory=_event_id)
    created_at: datetime = field(default_factory=_now)
    kind: EventKind = field(init=False, default=EventKind.CONTROL)


EventHandler = Callable[[Any], Awaitable[None]]


class AsyncEventBus:
    """FIFO event bus with typed subscriptions and explicit dead letters."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.handlers: Dict[EventKind, List[EventHandler]] = {}
        self.event_log: List[Any] = []
        self.dead_letters: List[Dict[str, Any]] = []
        self.running = False
        self.drain_lock = asyncio.Lock()

    def subscribe(self, kind: EventKind, handler: EventHandler) -> None:
        handlers = self.handlers.setdefault(kind, [])
        if handler not in handlers:
            handlers.append(handler)

    async def publish(self, event: Any) -> None:
        if not hasattr(event, "kind"):
            raise ValueError("event must expose EventKind as kind")
        await self.queue.put(event)

    async def process_one(self) -> bool:
        if self.queue.empty():
            return False
        event = await self.queue.get()
        self.event_log.append(event)
        try:
            for handler in self.handlers.get(event.kind, []):
                await handler(event)
        except Exception as exc:
            self.dead_letters.append({"event": event, "error": str(exc)})
            raise
        finally:
            self.queue.task_done()
        return True

    async def drain(self, max_events: int = 10_000) -> int:
        async with self.drain_lock:
            processed = 0
            while not self.queue.empty():
                if processed >= max_events:
                    raise RuntimeError("event bus exceeded max_events; possible event loop")
                await self.process_one()
                processed += 1
            return processed

    async def run(self) -> None:
        self.running = True
        while self.running:
            event = await self.queue.get()
            self.event_log.append(event)
            try:
                for handler in self.handlers.get(event.kind, []):
                    await handler(event)
            except Exception as exc:
                self.dead_letters.append({"event": event, "error": str(exc)})
            finally:
                self.queue.task_done()

    def stop(self) -> None:
        self.running = False
