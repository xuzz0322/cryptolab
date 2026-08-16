from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from .instruments import Instrument
from .models import BacktestConfig, Bar
from .order_models import Order, OrderType, Side


@dataclass(frozen=True)
class RuleDecision:
    allowed: bool
    reason: str = ""


class MarketRules(ABC):
    name = "base"
    settlement_days = 0

    @abstractmethod
    def validate_order(self, order: Order, reference_price: float) -> RuleDecision: ...

    @abstractmethod
    def is_locked(self, order: Order, bar: Bar, previous_close: Optional[float]) -> RuleDecision: ...

    def normalize_price(self, instrument: Instrument, price: float) -> float:
        return instrument.normalize_price(price)

    def normalize_quantity(self, instrument: Instrument, quantity: float) -> float:
        return instrument.normalize_quantity(quantity)

    def fee_rate(self, order: Order, config: BacktestConfig) -> float:
        return config.maker_fee_rate if order.order_type == OrderType.LIMIT else config.taker_fee_rate


class CryptoSpotRules(MarketRules):
    name = "crypto_spot"
    settlement_days = 0

    def __init__(self, instrument: Instrument):
        self.instrument = instrument

    def validate_order(self, order: Order, reference_price: float) -> RuleDecision:
        normalized = self.instrument.normalize_quantity(order.quantity)
        if normalized < float(self.instrument.minimum_quantity):
            return RuleDecision(False, "订单数量低于币对最小数量")
        price = order.limit_price if order.order_type == OrderType.LIMIT else reference_price
        if price is None or normalized * price < float(self.instrument.minimum_notional):
            return RuleDecision(False, "订单名义金额低于交易所最小值")
        return RuleDecision(True, "币对精度及最小金额检查通过")

    def is_locked(self, order: Order, bar: Bar, previous_close: Optional[float]) -> RuleDecision:
        del order, previous_close
        return RuleDecision(bar.volume > 0, "交易对无成交量" if bar.volume <= 0 else "")


class AShareRules(MarketRules):
    """Kept as an adapter for the later stock-market migration."""

    name = "a_share"
    settlement_days = 1

    def __init__(self, price_limit_pct: float = 0.10):
        self.price_limit_pct = price_limit_pct

    def validate_order(self, order: Order, reference_price: float) -> RuleDecision:
        del reference_price
        if order.side == Side.BUY and int(order.quantity) % 100:
            return RuleDecision(False, "A股买入数量必须为100股整手")
        return RuleDecision(True, "A股交易单位检查通过")

    def is_locked(self, order: Order, bar: Bar, previous_close: Optional[float]) -> RuleDecision:
        if bar.volume <= 0:
            return RuleDecision(False, "停牌或无成交量")
        if previous_close:
            limit_up = previous_close * (1 + self.price_limit_pct)
            limit_down = previous_close * (1 - self.price_limit_pct)
            if order.side == Side.BUY and bar.low >= limit_up:
                return RuleDecision(False, "涨停封板，无法买入")
            if order.side == Side.SELL and bar.high <= limit_down:
                return RuleDecision(False, "跌停封板，无法卖出")
        return RuleDecision(True, "")
