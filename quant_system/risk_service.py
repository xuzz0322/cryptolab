"""Independent pre-trade risk service with no strategy or exchange dependency."""

from dataclasses import dataclass, field
from typing import List, Optional, Set

from .events import OrderIntentEvent
from .order_models import Side


@dataclass
class RiskLimits:
    allowed_symbols: Set[str] = field(default_factory=lambda: {"BTC/USDT", "ETH/USDT", "SOL/USDT"})
    max_position_weight: float = 0.20
    max_order_notional: float = 500.0
    min_cash_reserve_pct: float = 0.10
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.10
    max_orders_per_day: int = 3
    max_market_age_seconds: float = 120.0

    def validate(self) -> None:
        if not 0 < self.max_position_weight <= 1:
            raise ValueError("max_position_weight must be in (0, 1]")
        if self.max_order_notional <= 0:
            raise ValueError("max_order_notional must be positive")
        if not 0 <= self.min_cash_reserve_pct < 1:
            raise ValueError("min_cash_reserve_pct must be in [0, 1)")
        if not 0 < self.max_daily_loss_pct < 1 or not 0 < self.max_drawdown_pct < 1:
            raise ValueError("loss limits must be in (0, 1)")
        if self.max_orders_per_day < 1 or self.max_market_age_seconds <= 0:
            raise ValueError("risk limits must be positive")


@dataclass(frozen=True)
class RiskContext:
    equity: float
    cash: float
    current_quantity: float
    available_quantity: float
    current_position_value: float
    daily_return: float
    drawdown: float
    orders_today: int
    market_age_seconds: float


@dataclass(frozen=True)
class IndependentRiskResult:
    approved: bool
    reasons: List[str]
    approved_quantity: float


class IndependentRiskService:
    """A deterministic, fail-closed risk boundary used before execution."""

    def __init__(self, limits: Optional[RiskLimits] = None):
        self.limits = limits or RiskLimits()
        self.limits.validate()
        self.kill_switch = False

    def set_kill_switch(self, enabled: bool) -> None:
        self.kill_switch = enabled

    def evaluate(self, intent: OrderIntentEvent, context: RiskContext) -> IndependentRiskResult:
        reasons: List[str] = []
        notional = intent.quantity * intent.reference_price
        if self.kill_switch:
            reasons.append("独立风控 Kill Switch 已开启")
        if intent.symbol not in self.limits.allowed_symbols:
            reasons.append("币对不在风控白名单")
        if intent.quantity <= 0 or intent.reference_price <= 0:
            reasons.append("订单数量或参考价格无效")
        if context.market_age_seconds > self.limits.max_market_age_seconds:
            reasons.append("行情已过期")
        if notional > self.limits.max_order_notional + 1e-9:
            reasons.append("订单名义金额超过独立风控上限")
        if context.orders_today >= self.limits.max_orders_per_day:
            reasons.append("达到单日订单数量上限")
        if context.daily_return <= -self.limits.max_daily_loss_pct and intent.side == Side.BUY:
            reasons.append("触发单日亏损熔断")
        if context.drawdown <= -self.limits.max_drawdown_pct and intent.side == Side.BUY:
            reasons.append("触发最大回撤熔断")
        if intent.side == Side.SELL and intent.quantity > context.available_quantity + 1e-12:
            reasons.append("卖出数量超过可用持仓")
        if intent.side == Side.BUY:
            projected_position = context.current_position_value + notional
            if context.equity <= 0 or projected_position / context.equity > self.limits.max_position_weight + 1e-9:
                reasons.append("下单后仓位超过独立风控上限")
            projected_cash = context.cash - notional
            if projected_cash < context.equity * self.limits.min_cash_reserve_pct:
                reasons.append("下单后现金储备低于下限")
        return IndependentRiskResult(not reasons, reasons or ["独立风控检查通过"], intent.quantity if not reasons else 0.0)
