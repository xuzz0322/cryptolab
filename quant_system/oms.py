import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .order_models import ACTIVE_STATUSES, Fill, Order, OrderStatus, OrderType, Side


ALLOWED_TRANSITIONS = {
    OrderStatus.CREATED: {OrderStatus.RISK_CHECKED, OrderStatus.REJECTED, OrderStatus.CANCELLED},
    OrderStatus.RISK_CHECKED: {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELLED},
    OrderStatus.SUBMITTED: {OrderStatus.ACCEPTED, OrderStatus.REJECTED, OrderStatus.CANCELLED},
    OrderStatus.ACCEPTED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED},
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED},
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
}


class OrderRepository(ABC):
    @abstractmethod
    def save_order(self, order: Order) -> None: ...

    @abstractmethod
    def save_fill(self, fill: Fill) -> None: ...

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]: ...

    @abstractmethod
    def list_orders(self) -> List[Order]: ...

    @abstractmethod
    def list_fills(self) -> List[Fill]: ...


class InMemoryOrderRepository(OrderRepository):
    def __init__(self):
        self.orders: Dict[str, Order] = {}
        self.fills: List[Fill] = []

    def save_order(self, order: Order) -> None:
        self.orders[order.order_id] = order

    def save_fill(self, fill: Fill) -> None:
        self.fills.append(fill)

    def get_order(self, order_id: str) -> Optional[Order]:
        return self.orders.get(order_id)

    def list_orders(self) -> List[Order]:
        return list(self.orders.values())

    def list_fills(self) -> List[Fill]:
        return list(self.fills)


class SqliteOrderRepository(OrderRepository):
    """Durable OMS event store suitable for local simulation and recovery."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS fills (fill_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.connection.commit()

    @staticmethod
    def _order(payload: str) -> Order:
        item = json.loads(payload)
        item["trading_date"] = date.fromisoformat(item["trading_date"])
        item["side"] = Side(item["side"])
        item["order_type"] = OrderType(item["order_type"])
        item["status"] = OrderStatus(item["status"])
        return Order(**item)

    @staticmethod
    def _fill(payload: str) -> Fill:
        item = json.loads(payload)
        item["trading_date"] = date.fromisoformat(item["trading_date"])
        item["side"] = Side(item["side"])
        return Fill(**item)

    def save_order(self, order: Order) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO orders(order_id, payload) VALUES (?, ?)",
            (order.order_id, json.dumps(order.to_dict(), ensure_ascii=False)),
        )
        self.connection.commit()

    def save_fill(self, fill: Fill) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO fills(fill_id, payload) VALUES (?, ?)",
            (fill.fill_id, json.dumps(fill.to_dict(), ensure_ascii=False)),
        )
        self.connection.commit()

    def get_order(self, order_id: str) -> Optional[Order]:
        row = self.connection.execute("SELECT payload FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        return self._order(row[0]) if row else None

    def list_orders(self) -> List[Order]:
        return [self._order(row[0]) for row in self.connection.execute("SELECT payload FROM orders ORDER BY rowid")]

    def list_fills(self) -> List[Fill]:
        return [self._fill(row[0]) for row in self.connection.execute("SELECT payload FROM fills ORDER BY rowid")]

    def close(self) -> None:
        self.connection.close()


class OrderManager:
    def __init__(self, repository: Optional[OrderRepository] = None):
        self.repository = repository or InMemoryOrderRepository()

    def create(self, order: Order) -> Order:
        if order.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if order.order_type == OrderType.LIMIT and (order.limit_price is None or order.limit_price <= 0):
            raise ValueError("limit order requires positive limit_price")
        duplicate = next(
            (item for item in self.repository.list_orders() if item.client_order_id == order.client_order_id), None
        )
        if duplicate:
            return duplicate
        self.repository.save_order(order)
        return order

    def transition(self, order: Order, status: OrderStatus, reason: str = "") -> None:
        if status not in ALLOWED_TRANSITIONS[order.status]:
            raise ValueError("invalid order transition: %s -> %s" % (order.status.value, status.value))
        order.status = status
        if status == OrderStatus.REJECTED:
            order.reject_reason = reason
        self.repository.save_order(order)

    def approve_and_submit(self, order: Order) -> None:
        self.transition(order, OrderStatus.RISK_CHECKED)
        self.transition(order, OrderStatus.SUBMITTED)
        self.transition(order, OrderStatus.ACCEPTED)

    def reject(self, order: Order, reason: str) -> None:
        self.transition(order, OrderStatus.REJECTED, reason)

    def record_fill(self, order: Order, fill: Fill) -> None:
        if order.status not in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}:
            raise ValueError("order cannot receive fill in status %s" % order.status.value)
        if fill.quantity <= 0 or fill.quantity > order.remaining_quantity:
            raise ValueError("invalid fill quantity")
        previous_value = order.average_fill_price * order.filled_quantity
        order.filled_quantity += fill.quantity
        order.average_fill_price = (previous_value + fill.quantity * fill.price) / order.filled_quantity
        order.commission += fill.commission
        self.repository.save_fill(fill)
        target = OrderStatus.FILLED if order.remaining_quantity <= 1e-12 else OrderStatus.PARTIALLY_FILLED
        self.transition(order, target)

    def cancel(self, order: Order, reason: str = "用户撤单") -> None:
        if order.status not in ACTIVE_STATUSES:
            return
        order.reason = (order.reason + "；" + reason).strip("；")
        self.transition(order, OrderStatus.CANCELLED)

    def cancel_active(self, symbol: Optional[str] = None, reason: str = "策略信号更新") -> None:
        for order in self.repository.list_orders():
            if order.status in ACTIVE_STATUSES and (symbol is None or order.symbol == symbol):
                self.cancel(order, reason)
