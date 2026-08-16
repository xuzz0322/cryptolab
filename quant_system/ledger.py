"""Exchange-authoritative ledger snapshots and reconciliation."""

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Set

from .events import OrderUpdateEvent, ReconciliationEvent
from .exchange import AccountSnapshot, ExchangeAdapter
from .instruments import Instrument
from .order_models import ACTIVE_STATUSES


@dataclass
class LedgerState:
    equity: float = 0.0
    cash: float = 0.0
    base_quantity: float = 0.0
    available_quantity: float = 0.0
    position_value: float = 0.0
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    daily_return: float = 0.0
    drawdown: float = 0.0
    trading_date: Optional[date] = None
    orders_today: int = 0


class LedgerReconciliationService:
    """Treat remote balances/open orders as truth and report every divergence."""

    def __init__(self):
        self.state = LedgerState()
        self.expected_active_order_ids: Set[str] = set()
        self.audit_log: List[ReconciliationEvent] = []

    def record_order_update(self, event: OrderUpdateEvent) -> None:
        order = event.order
        if order.status in ACTIVE_STATUSES:
            self.expected_active_order_ids.add(order.order_id)
        else:
            self.expected_active_order_ids.discard(order.order_id)
        self.state.orders_today += 1

    async def reconcile(
        self,
        adapter: ExchangeAdapter,
        instrument: Instrument,
        mark_price: float,
        trading_date: date,
    ) -> ReconciliationEvent:
        snapshot = await adapter.reconcile()
        balances = {item.currency: item for item in snapshot.balances}
        quote = balances.get(instrument.quote_currency)
        base = balances.get(instrument.base_currency)
        cash = quote.total if quote else 0.0
        base_quantity = base.total if base else 0.0
        available = base.free if base else 0.0
        equity = cash + base_quantity * mark_price
        if self.state.trading_date != trading_date:
            self.state.trading_date = trading_date
            self.state.day_start_equity = equity
            self.state.orders_today = 0
        self.state.cash = cash
        self.state.base_quantity = base_quantity
        self.state.available_quantity = available
        self.state.position_value = base_quantity * mark_price
        self.state.equity = equity
        self.state.peak_equity = max(self.state.peak_equity, equity)
        self.state.daily_return = equity / self.state.day_start_equity - 1 if self.state.day_start_equity else 0.0
        self.state.drawdown = equity / self.state.peak_equity - 1 if self.state.peak_equity else 0.0

        remote_ids = {order.order_id for order in snapshot.open_orders}
        missing = self.expected_active_order_ids - remote_ids
        unexpected = remote_ids - self.expected_active_order_ids
        discrepancies = ["本地活动订单在交易所不存在: %s" % item for item in sorted(missing)]
        discrepancies += ["交易所存在本地未知订单: %s" % item for item in sorted(unexpected)]
        event = ReconciliationEvent(snapshot, discrepancies)
        self.audit_log.append(event)
        return event
