"""Market-independent asynchronous exchange adapter contracts."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from .instruments import Instrument
from .models import Bar
from .order_models import Fill, Order


class ExchangeError(RuntimeError):
    pass


class AuthenticationError(ExchangeError):
    pass


class TradingDisabledError(ExchangeError):
    pass


class AdapterNotConnectedError(ExchangeError):
    pass


@dataclass(frozen=True)
class Balance:
    currency: str
    free: float
    locked: float = 0.0

    @property
    def total(self) -> float:
        return self.free + self.locked


@dataclass(frozen=True)
class ExchangePosition:
    symbol: str
    quantity: float
    available_quantity: float
    average_cost: float = 0.0
    mark_price: float = 0.0


@dataclass
class AccountSnapshot:
    venue: str
    balances: List[Balance]
    positions: List[ExchangePosition]
    open_orders: List[Order]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "balances": [asdict(item) for item in self.balances],
            "positions": [asdict(item) for item in self.positions],
            "open_orders": [item.to_dict() for item in self.open_orders],
        }


class UserEventKind(str, Enum):
    ORDER = "ORDER"
    TRADE = "TRADE"
    BALANCE = "BALANCE"
    LISTEN_KEY_EXPIRED = "LISTEN_KEY_EXPIRED"


@dataclass(frozen=True)
class ExchangeUserEvent:
    kind: UserEventKind
    venue: str
    order: Optional[Order] = None
    fill: Optional[Fill] = None
    balances: List[Balance] = field(default_factory=list)
    raw_event_type: str = ""
    event_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ExchangeAdapter(ABC):
    venue = "ABSTRACT"

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    async def submit_order(self, order: Order) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Order: ...

    @abstractmethod
    async def get_order(self, order_id: str, symbol: Optional[str] = None) -> Optional[Order]: ...

    @abstractmethod
    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Order]: ...

    @abstractmethod
    async def get_balances(self) -> List[Balance]: ...

    @abstractmethod
    async def get_positions(self) -> List[ExchangePosition]: ...

    @abstractmethod
    async def get_fills(self, symbol: Optional[str] = None) -> List[Fill]: ...

    @abstractmethod
    async def get_instruments(self) -> Sequence[Instrument]: ...

    @abstractmethod
    async def stream_bars(self) -> AsyncIterator[Bar]:
        """Yield normalized market-data events until disconnected."""
        if False:
            yield Bar  # pragma: no cover

    async def stream_user_events(self) -> AsyncIterator[ExchangeUserEvent]:
        """Yield private account/order/trade updates."""
        if False:
            yield ExchangeUserEvent  # pragma: no cover
        raise ExchangeError("this adapter does not provide a user event stream")

    async def reconcile(self) -> AccountSnapshot:
        balances = await self.get_balances()
        positions = await self.get_positions()
        open_orders = await self.get_open_orders()
        return AccountSnapshot(self.venue, balances, positions, open_orders)


class ExecutionRouter:
    def __init__(self):
        self.adapters: Dict[str, ExchangeAdapter] = {}

    def register(self, adapter: ExchangeAdapter) -> None:
        key = adapter.venue.upper()
        if key in self.adapters:
            raise ValueError("exchange adapter already registered: %s" % key)
        self.adapters[key] = adapter

    def get(self, venue: str) -> ExchangeAdapter:
        key = venue.upper()
        if key not in self.adapters:
            raise ValueError("no exchange adapter registered for %s" % venue)
        return self.adapters[key]

    async def submit_order(self, venue: str, order: Order) -> Order:
        return await self.get(venue).submit_order(order)

    async def cancel_order(self, venue: str, order_id: str, symbol: Optional[str] = None) -> Order:
        return await self.get(venue).cancel_order(order_id, symbol)
