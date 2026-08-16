from dataclasses import dataclass
from typing import Optional

from .models import BacktestConfig, Bar
from .instruments import Instrument
from .market_rules import MarketRules
from .oms import OrderManager
from .order_models import Fill, Order, OrderStatus, OrderType, Side
from .portfolio import Portfolio


@dataclass
class ExecutionResult:
    fill: Optional[Fill] = None
    message: str = ""


class SimulatedBroker:
    """Deterministic daily-bar broker with limits, liquidity and partial fills."""

    def __init__(self, config: BacktestConfig, order_manager: OrderManager, rules: MarketRules, instrument: Instrument):
        self.config = config
        self.order_manager = order_manager
        self.rules = rules
        self.instrument = instrument

    def _commission(self, order: Order, value: float) -> float:
        commission = max(value * self.rules.fee_rate(order, self.config), self.config.minimum_commission)
        if order.side == Side.SELL:
            commission += value * self.config.sell_tax_rate
        return commission

    def execute(
        self,
        order: Order,
        bar: Bar,
        portfolio: Portfolio,
        previous_close: Optional[float] = None,
    ) -> ExecutionResult:
        if order.status != OrderStatus.ACCEPTED and order.status != OrderStatus.PARTIALLY_FILLED:
            return ExecutionResult(message="订单状态不可撮合")
        market_decision = self.rules.is_locked(order, bar, previous_close)
        if not market_decision.allowed:
            if order.status == OrderStatus.PARTIALLY_FILLED:
                self.order_manager.cancel(order, market_decision.reason)
            else:
                self.order_manager.reject(order, market_decision.reason)
            return ExecutionResult(message=market_decision.reason)

        if order.order_type == OrderType.LIMIT:
            if order.side == Side.BUY and (order.limit_price is None or bar.low > order.limit_price):
                return ExecutionResult(message="限价买单未触及")
            if order.side == Side.SELL and (order.limit_price is None or bar.high < order.limit_price):
                return ExecutionResult(message="限价卖单未触及")
            raw_price = min(bar.open, order.limit_price) if order.side == Side.BUY else max(bar.open, order.limit_price)
        else:
            raw_price = bar.open
        fill_price = self.rules.normalize_price(self.instrument, raw_price * (
            1 + self.config.slippage_rate if order.side == Side.BUY else 1 - self.config.slippage_rate
        ))
        liquidity = self.rules.normalize_quantity(
            self.instrument, bar.volume * self.config.max_volume_participation
        )
        fill_quantity = min(order.remaining_quantity, liquidity)
        if order.side == Side.BUY:
            cash_after_minimum = max(0.0, portfolio.cash - self.config.minimum_commission)
            estimated_per_share = fill_price * (1 + self.rules.fee_rate(order, self.config))
            affordable = cash_after_minimum / estimated_per_share
            affordable = self.rules.normalize_quantity(self.instrument, affordable)
            fill_quantity = min(fill_quantity, affordable)
        else:
            fill_quantity = min(fill_quantity, portfolio.position(order.symbol).available_quantity)
        fill_quantity = self.rules.normalize_quantity(self.instrument, fill_quantity)
        if fill_quantity <= 0 or fill_quantity * fill_price < float(self.instrument.minimum_notional):
            return ExecutionResult(message="流动性、资金或可用持仓不足")
        value = fill_quantity * fill_price
        fill = Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill_quantity,
            price=fill_price,
            commission=self._commission(order, value),
            trading_date=bar.date,
        )
        portfolio.apply_fill(fill)
        self.order_manager.record_fill(order, fill)
        return ExecutionResult(fill=fill, message="成交")
