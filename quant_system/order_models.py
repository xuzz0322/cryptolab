from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    RISK_CHECKED = "RISK_CHECKED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


ACTIVE_STATUSES = {
    OrderStatus.CREATED,
    OrderStatus.RISK_CHECKED,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
}


@dataclass
class Order:
    symbol: str
    side: Side
    quantity: float
    trading_date: date
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    reason: str = ""
    client_order_id: str = field(default_factory=lambda: uuid4().hex)
    order_id: str = field(default_factory=lambda: uuid4().hex)
    exchange_order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: float = 0.0
    average_fill_price: float = 0.0
    commission: float = 0.0
    reject_reason: str = ""

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["trading_date"] = self.trading_date.isoformat()
        result["side"] = self.side.value
        result["order_type"] = self.order_type.value
        result["status"] = self.status.value
        return result


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    commission: float
    trading_date: date
    fill_id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["trading_date"] = self.trading_date.isoformat()
        result["side"] = self.side.value
        return result
