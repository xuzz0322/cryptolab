from dataclasses import dataclass
from typing import Optional

from .models import BacktestConfig, Signal
from .order_models import Order, OrderType, Side
from .portfolio import Portfolio


@dataclass
class RiskDecision:
    target_weight: float
    reason: str


class RiskManager:
    """Pre-trade controls: position cap, long-only constraint and stop loss."""

    def __init__(self, config: BacktestConfig):
        self.config = config

    def evaluate(
        self,
        signal: Signal,
        current_price: float,
        average_entry_price: Optional[float],
        has_position: bool,
    ) -> RiskDecision:
        target = min(max(signal.target_weight, 0.0), self.config.max_position_weight)
        reason = signal.reason
        if (
            has_position
            and average_entry_price
            and self.config.stop_loss_pct is not None
            and current_price <= average_entry_price * (1 - self.config.stop_loss_pct)
        ):
            return RiskDecision(0.0, "触发 %.1f%% 止损" % (self.config.stop_loss_pct * 100))
        if target != signal.target_weight:
            reason += "；仓位经风控限制为 %.0f%%" % (target * 100)
        return RiskDecision(target, reason)

    def check_order(self, order: Order, portfolio: Portfolio, reference_price: float) -> RiskDecision:
        """Return an order-level pre-trade decision. A zero weight means rejection."""
        if order.quantity <= 0 or reference_price <= 0:
            return RiskDecision(0.0, "订单数量或参考价格无效")
        units = order.quantity / self.config.lot_size
        if order.side == Side.BUY and abs(units - round(units)) > 1e-7:
            return RiskDecision(0.0, "买入数量不符合最小数量步长")
        order_value = order.quantity * reference_price
        if order_value < self.config.minimum_notional:
            return RiskDecision(0.0, "订单名义金额低于最小值")
        if self.config.max_order_value is not None and order_value > self.config.max_order_value:
            return RiskDecision(0.0, "订单金额超过单笔上限")
        if portfolio.daily_return <= -self.config.max_daily_loss_pct and order.side == Side.BUY:
            return RiskDecision(0.0, "触发单日亏损熔断，禁止开新仓")
        if portfolio.drawdown <= -self.config.max_drawdown_halt_pct and order.side == Side.BUY:
            return RiskDecision(0.0, "触发组合回撤熔断，禁止开新仓")
        position = portfolio.position(order.symbol)
        if order.side == Side.SELL and order.quantity > position.available_quantity:
            return RiskDecision(0.0, "卖出数量超过当前可用持仓")
        if order.side == Side.BUY:
            projected_value = position.quantity * reference_price + order_value
            projected_equity = max(portfolio.equity, 1.0)
            if projected_value / projected_equity > self.config.max_position_weight + 1e-9:
                return RiskDecision(0.0, "下单后单股仓位超过上限")
            estimated_cost = order_value * (1 + self.config.commission_rate + self.config.slippage_rate)
            if estimated_cost > portfolio.cash + 1e-8:
                return RiskDecision(0.0, "可用资金不足")
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            deviation = abs(order.limit_price / reference_price - 1)
            if deviation > 0.20:
                return RiskDecision(0.0, "限价偏离参考价格超过 20%")
        return RiskDecision(1.0, "风控检查通过")
