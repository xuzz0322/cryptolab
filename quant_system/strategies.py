from abc import ABC, abstractmethod
from typing import Dict, List, Sequence, Type

from .indicators import momentum, realized_volatility, rolling_std, rsi, sma
from .models import Bar, Signal


class Strategy(ABC):
    name = "base"

    @abstractmethod
    def generate_signals(self, bars: Sequence[Bar]) -> List[Signal]:
        """Return a target portfolio weight for each close."""


class MovingAverageCrossStrategy(Strategy):
    name = "ma_cross"

    def __init__(self, short_window: int = 20, long_window: int = 60):
        if not 1 <= short_window < long_window:
            raise ValueError("require 1 <= short_window < long_window")
        self.short_window = short_window
        self.long_window = long_window

    def generate_signals(self, bars: Sequence[Bar]) -> List[Signal]:
        prices = [bar.close for bar in bars]
        short = sma(prices, self.short_window)
        long = sma(prices, self.long_window)
        signals = []
        for fast, slow in zip(short, long):
            if fast is None or slow is None:
                signals.append(Signal(0.0, "均线预热"))
            elif fast > slow:
                signals.append(Signal(1.0, "短期均线上穿长期均线"))
            else:
                signals.append(Signal(0.0, "短期均线位于长期均线下方"))
        return signals


class RsiMeanReversionStrategy(Strategy):
    name = "rsi_reversion"

    def __init__(self, window: int = 14, buy_below: float = 35, sell_above: float = 65):
        if not 0 < buy_below < sell_above < 100:
            raise ValueError("require 0 < buy_below < sell_above < 100")
        self.window, self.buy_below, self.sell_above = window, buy_below, sell_above

    def generate_signals(self, bars: Sequence[Bar]) -> List[Signal]:
        values = rsi([bar.close for bar in bars], self.window)
        holding = False
        result = []
        for value in values:
            if value is None:
                result.append(Signal(0.0, "RSI 预热"))
            elif value < self.buy_below:
                holding = True
                result.append(Signal(1.0, "RSI 超卖，预期均值回归"))
            elif value > self.sell_above:
                holding = False
                result.append(Signal(0.0, "RSI 超买，退出仓位"))
            else:
                result.append(Signal(1.0 if holding else 0.0, "RSI 中性，保持仓位"))
        return result


class BollingerMeanReversionStrategy(Strategy):
    name = "bollinger"

    def __init__(self, window: int = 20, std_multiplier: float = 2.0):
        if window < 2 or std_multiplier <= 0:
            raise ValueError("invalid Bollinger parameters")
        self.window, self.std_multiplier = window, std_multiplier

    def generate_signals(self, bars: Sequence[Bar]) -> List[Signal]:
        prices = [bar.close for bar in bars]
        middle, deviations = sma(prices, self.window), rolling_std(prices, self.window)
        holding = False
        result = []
        for price, average, deviation in zip(prices, middle, deviations):
            if average is None or deviation is None:
                result.append(Signal(0.0, "布林带预热"))
            elif price < average - self.std_multiplier * deviation:
                holding = True
                result.append(Signal(1.0, "价格跌破布林带下轨"))
            elif price >= average:
                holding = False
                result.append(Signal(0.0, "价格回归中轨"))
            else:
                result.append(Signal(1.0 if holding else 0.0, "等待价格回归"))
        return result


class RegimeTrendStrategy(Strategy):
    """Long-only trend strategy with momentum, volatility and exit hysteresis."""

    name = "regime_trend"

    def __init__(
        self,
        long_window: int = 200,
        momentum_window: int = 90,
        volatility_window: int = 20,
        entry_momentum: float = 0.05,
        exit_momentum: float = -0.02,
        entry_trend_buffer: float = 0.01,
        exit_trend_buffer: float = 0.01,
        max_annualized_volatility: float = 0.80,
        exit_volatility_multiplier: float = 1.10,
        target_weight: float = 1.0,
    ):
        if long_window < 2 or momentum_window < 1 or volatility_window < 2:
            raise ValueError("regime windows are too short")
        if long_window < max(momentum_window, volatility_window):
            raise ValueError("long_window must cover momentum and volatility windows")
        if exit_momentum >= entry_momentum:
            raise ValueError("exit_momentum must be below entry_momentum")
        if not 0 <= entry_trend_buffer < 1 or not 0 <= exit_trend_buffer < 1:
            raise ValueError("trend buffers must be in [0, 1)")
        if max_annualized_volatility <= 0 or exit_volatility_multiplier < 1:
            raise ValueError("volatility limits are invalid")
        if not 0 < target_weight <= 1:
            raise ValueError("target_weight must be in (0, 1]")
        self.long_window = long_window
        self.momentum_window = momentum_window
        self.volatility_window = volatility_window
        self.entry_momentum = entry_momentum
        self.exit_momentum = exit_momentum
        self.entry_trend_buffer = entry_trend_buffer
        self.exit_trend_buffer = exit_trend_buffer
        self.max_annualized_volatility = max_annualized_volatility
        self.exit_volatility_multiplier = exit_volatility_multiplier
        self.target_weight = target_weight

    def generate_signals(self, bars: Sequence[Bar]) -> List[Signal]:
        prices = [bar.close for bar in bars]
        long_average = sma(prices, self.long_window)
        momentum_values = momentum(prices, self.momentum_window)
        volatility_values = realized_volatility(prices, self.volatility_window, 365)
        holding = False
        signals: List[Signal] = []
        for price, average, momentum_value, volatility in zip(
            prices, long_average, momentum_values, volatility_values
        ):
            if average is None or momentum_value is None or volatility is None:
                holding = False
                signals.append(Signal(0.0, "市场状态策略预热"))
                continue
            entry_trend = price > average * (1 + self.entry_trend_buffer)
            exit_trend = price < average * (1 - self.exit_trend_buffer)
            entry = (
                entry_trend
                and momentum_value > self.entry_momentum
                and volatility <= self.max_annualized_volatility
            )
            exit_position = (
                exit_trend
                or momentum_value < self.exit_momentum
                or volatility > self.max_annualized_volatility * self.exit_volatility_multiplier
            )
            if not holding and entry:
                holding = True
                reason = "长期趋势向上、中期动量为正且波动率合格"
            elif holding and exit_position:
                holding = False
                reason = "趋势、动量或波动率触发退出"
            elif holding:
                reason = "处于进入与退出阈值之间，滞回保持仓位"
            else:
                reason = "市场状态未满足进入条件"
            signals.append(Signal(self.target_weight if holding else 0.0, reason))
        return signals


STRATEGIES: Dict[str, Type[Strategy]] = {
    MovingAverageCrossStrategy.name: MovingAverageCrossStrategy,
    RsiMeanReversionStrategy.name: RsiMeanReversionStrategy,
    BollingerMeanReversionStrategy.name: BollingerMeanReversionStrategy,
    RegimeTrendStrategy.name: RegimeTrendStrategy,
}

# Imported after Strategy is defined; AIModelStrategy intentionally depends
# only on Bar/Signal and cannot access execution or account objects.
from .ai_strategy import AIModelStrategy

STRATEGIES[AIModelStrategy.name] = AIModelStrategy


def create_strategy(name: str, params: Dict[str, float] = None) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError("unknown strategy: %s" % name)
    return STRATEGIES[name](**(params or {}))
