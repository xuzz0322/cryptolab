import asyncio
from typing import AsyncIterator, List, Optional, Sequence

from ..broker import SimulatedBroker
from ..exchange import (
    AdapterNotConnectedError,
    Balance,
    ExchangeAdapter,
    ExchangePosition,
    TradingDisabledError,
    ExchangeUserEvent,
    UserEventKind,
)
from ..instruments import Instrument, get_instrument
from ..market_rules import CryptoSpotRules
from ..models import BacktestConfig, Bar
from ..oms import InMemoryOrderRepository, OrderManager, OrderRepository
from ..order_models import ACTIVE_STATUSES, Fill, Order, OrderStatus
from ..portfolio import Portfolio
from ..risk import RiskManager


class PaperExchangeAdapter(ExchangeAdapter):
    """In-process spot exchange with the same async interface as a live venue."""

    venue = "PAPER"

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        initial_cash: float = 100_000.0,
        config: Optional[BacktestConfig] = None,
        repository: Optional[OrderRepository] = None,
    ):
        self.instrument = get_instrument(symbol)
        self.config = config or BacktestConfig(initial_cash=initial_cash)
        self.config.validate()
        self.rules = CryptoSpotRules(self.instrument)
        self.repository = repository or InMemoryOrderRepository()
        self.oms = OrderManager(self.repository)
        self.portfolio = Portfolio(self.config.initial_cash, settlement_days=0)
        self.risk = RiskManager(self.config)
        self.broker = SimulatedBroker(self.config, self.oms, self.rules, self.instrument)
        self._connected = False
        self._kill_switch = False
        self._bars: dict = {}
        self._previous_closes: dict = {}
        self._bar_queue: asyncio.Queue = asyncio.Queue()
        self._user_queue: asyncio.Queue = asyncio.Queue()

    @property
    def connected(self) -> bool:
        return self._connected

    def _require_connected(self) -> None:
        if not self._connected:
            raise AdapterNotConnectedError("paper exchange is not connected")

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def set_kill_switch(self, enabled: bool) -> None:
        self._kill_switch = enabled
        if enabled:
            self.oms.cancel_active(reason="触发全局 Kill Switch")

    async def publish_bar(self, bar: Bar, symbol: Optional[str] = None) -> None:
        self._require_connected()
        target = symbol or self.instrument.symbol
        previous = self._bars.get(target)
        if previous:
            self._previous_closes[target] = previous.close
        self._bars[target] = bar
        self.portfolio.start_day(bar.date, {target: bar.open})
        for order in list(self.repository.list_orders()):
            if order.symbol == target and order.status in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}:
                fills_before = len(self.repository.list_fills())
                self.broker.execute(order, bar, self.portfolio, self._previous_closes.get(target))
                await self._publish_order_events(order, fills_before)
        self.portfolio.mark({target: bar.close})
        await self._bar_queue.put(bar)

    async def submit_order(self, order: Order) -> Order:
        self._require_connected()
        if self._kill_switch:
            raise TradingDisabledError("Kill Switch 已开启，禁止下单")
        bar = self._bars.get(order.symbol)
        created = self.oms.create(order)
        if not bar:
            self.oms.reject(created, "尚无该币对行情")
            await self._publish_order_events(created, len(self.repository.list_fills()))
            return created
        fills_before = len(self.repository.list_fills())
        market_decision = self.rules.validate_order(created, bar.close)
        risk_decision = self.risk.check_order(created, self.portfolio, bar.close)
        if not market_decision.allowed:
            self.oms.reject(created, market_decision.reason)
        elif risk_decision.target_weight == 0:
            self.oms.reject(created, risk_decision.reason)
        else:
            self.oms.approve_and_submit(created)
            self.broker.execute(created, bar, self.portfolio, self._previous_closes.get(order.symbol))
        await self._publish_order_events(created, fills_before)
        return created

    async def _publish_order_events(self, order: Order, fills_before: int) -> None:
        await self._user_queue.put(
            ExchangeUserEvent(UserEventKind.ORDER, self.venue, order=order, raw_event_type="paperOrderUpdate")
        )
        for fill in self.repository.list_fills()[fills_before:]:
            await self._user_queue.put(
                ExchangeUserEvent(
                    UserEventKind.TRADE,
                    self.venue,
                    order=order,
                    fill=fill,
                    raw_event_type="paperTradeUpdate",
                )
            )

    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Order:
        del symbol
        self._require_connected()
        order = self.repository.get_order(order_id)
        if not order:
            raise ValueError("unknown order: %s" % order_id)
        self.oms.cancel(order)
        return order

    async def get_order(self, order_id: str, symbol: Optional[str] = None) -> Optional[Order]:
        del symbol
        self._require_connected()
        return self.repository.get_order(order_id)

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Order]:
        self._require_connected()
        return [
            order
            for order in self.repository.list_orders()
            if order.status in ACTIVE_STATUSES and (symbol is None or order.symbol == symbol)
        ]

    async def get_balances(self) -> List[Balance]:
        self._require_connected()
        position = self.portfolio.position(self.instrument.symbol)
        return [
            Balance(self.instrument.quote_currency, self.portfolio.cash),
            Balance(self.instrument.base_currency, position.available_quantity, position.quantity - position.available_quantity),
        ]

    async def get_positions(self) -> List[ExchangePosition]:
        self._require_connected()
        position = self.portfolio.position(self.instrument.symbol)
        if position.quantity <= 0:
            return []
        return [
            ExchangePosition(
                self.instrument.symbol,
                position.quantity,
                position.available_quantity,
                position.average_cost,
                position.last_price,
            )
        ]

    async def get_fills(self, symbol: Optional[str] = None) -> List[Fill]:
        self._require_connected()
        return [fill for fill in self.repository.list_fills() if symbol is None or fill.symbol == symbol]

    async def get_instruments(self) -> Sequence[Instrument]:
        return [self.instrument]

    async def stream_bars(self) -> AsyncIterator[Bar]:
        self._require_connected()
        while self._connected:
            yield await self._bar_queue.get()

    async def stream_user_events(self) -> AsyncIterator[ExchangeUserEvent]:
        self._require_connected()
        while self._connected:
            yield await self._user_queue.get()
