from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["date"] = self.date.isoformat()
        return result


@dataclass
class Signal:
    target_weight: float
    reason: str


@dataclass
class Trade:
    date: date
    side: str
    quantity: float
    price: float
    commission: float
    reason: str
    realized_pnl: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["date"] = self.date.isoformat()
        return result


@dataclass
class EquityPoint:
    date: date
    equity: float
    cash: float
    position_value: float
    drawdown: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["date"] = self.date.isoformat()
        return result


@dataclass
class BacktestConfig:
    initial_cash: float = 100_000.0
    commission_rate: float = 0.001
    slippage_rate: float = 0.0005
    max_position_weight: float = 0.95
    stop_loss_pct: Optional[float] = 0.08
    lot_size: float = 0.00001
    max_order_value: Optional[float] = None
    max_daily_loss_pct: float = 0.05
    max_drawdown_halt_pct: float = 0.20
    max_volume_participation: float = 0.10
    price_limit_pct: Optional[float] = None
    minimum_commission: float = 0.0
    sell_tax_rate: float = 0.0
    maker_fee_rate: float = 0.0008
    taker_fee_rate: float = 0.001
    settlement_days: int = 0
    minimum_notional: float = 5.0

    def validate(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if not 0 <= self.commission_rate < 1:
            raise ValueError("commission_rate must be in [0, 1)")
        if not 0 <= self.slippage_rate < 1:
            raise ValueError("slippage_rate must be in [0, 1)")
        if not 0 <= self.max_position_weight <= 1:
            raise ValueError("max_position_weight must be in [0, 1]")
        if self.stop_loss_pct is not None and not 0 < self.stop_loss_pct < 1:
            raise ValueError("stop_loss_pct must be in (0, 1)")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.max_order_value is not None and self.max_order_value <= 0:
            raise ValueError("max_order_value must be positive")
        if not 0 < self.max_daily_loss_pct < 1:
            raise ValueError("max_daily_loss_pct must be in (0, 1)")
        if not 0 < self.max_drawdown_halt_pct < 1:
            raise ValueError("max_drawdown_halt_pct must be in (0, 1)")
        if not 0 < self.max_volume_participation <= 1:
            raise ValueError("max_volume_participation must be in (0, 1]")
        if self.price_limit_pct is not None and not 0 < self.price_limit_pct < 1:
            raise ValueError("price_limit_pct must be in (0, 1)")
        if self.minimum_commission < 0 or not 0 <= self.sell_tax_rate < 1:
            raise ValueError("invalid fee configuration")
        if not 0 <= self.maker_fee_rate < 1 or not 0 <= self.taker_fee_rate < 1:
            raise ValueError("maker/taker fee rates must be in [0, 1)")
        if self.settlement_days not in (0, 1):
            raise ValueError("settlement_days must be 0 or 1")
        if self.minimum_notional < 0:
            raise ValueError("minimum_notional must be non-negative")
