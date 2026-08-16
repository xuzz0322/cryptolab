"""Model-driven strategy extension point.

Models only produce probabilities. They never receive credentials, account
objects or exchange adapters, so inference cannot bypass risk controls.
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence

from .indicators import returns, rsi, sma
from .models import Bar, Signal


@dataclass(frozen=True)
class FeatureVector:
    momentum_5: float
    momentum_20: float
    volatility_20: float
    rsi_14: float
    trend_20: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "momentum_5": self.momentum_5,
            "momentum_20": self.momentum_20,
            "volatility_20": self.volatility_20,
            "rsi_14": self.rsi_14,
            "trend_20": self.trend_20,
        }


class ProbabilityModel(ABC):
    """Adapter for sklearn, PyTorch, ONNX or a remote inference service."""

    @abstractmethod
    def predict_probability(self, features: FeatureVector) -> float: ...


class LinearProbabilityModel(ProbabilityModel):
    """Dependency-free reference model using a logistic output layer."""

    default_weights = {
        "momentum_5": 8.0,
        "momentum_20": 5.0,
        "volatility_20": -4.0,
        "rsi_14": 0.8,
        "trend_20": 8.0,
    }

    def __init__(self, weights: Optional[Mapping[str, float]] = None, bias: float = -0.15):
        self.weights = dict(weights or self.default_weights)
        self.bias = bias

    def predict_probability(self, features: FeatureVector) -> float:
        score = self.bias + sum(
            self.weights.get(name, 0.0) * value for name, value in features.to_dict().items()
        )
        score = max(-30.0, min(30.0, score))
        return 1.0 / (1.0 + math.exp(-score))


class AIModelStrategy:
    """Convert model probabilities into long-only target weights with hysteresis."""

    name = "ai_signal"

    def __init__(
        self,
        entry_probability: float = 0.55,
        exit_probability: float = 0.45,
        model: Optional[ProbabilityModel] = None,
    ):
        if not 0 < exit_probability < entry_probability < 1:
            raise ValueError("require 0 < exit_probability < entry_probability < 1")
        self.entry_probability = entry_probability
        self.exit_probability = exit_probability
        self.model = model or LinearProbabilityModel()

    @staticmethod
    def _features(prices: Sequence[float], index: int, rsi_values, averages) -> FeatureVector:
        recent_returns = [prices[i] / prices[i - 1] - 1 for i in range(index - 19, index + 1)]
        mean_return = sum(recent_returns) / len(recent_returns)
        volatility = math.sqrt(
            sum((value - mean_return) ** 2 for value in recent_returns) / len(recent_returns)
        )
        return FeatureVector(
            momentum_5=prices[index] / prices[index - 5] - 1,
            momentum_20=prices[index] / prices[index - 20] - 1,
            volatility_20=volatility,
            rsi_14=((rsi_values[index] or 50.0) - 50.0) / 50.0,
            trend_20=prices[index] / averages[index] - 1,
        )

    def generate_signals(self, bars: Sequence[Bar]) -> List[Signal]:
        prices = [bar.close for bar in bars]
        rsi_values = rsi(prices, 14)
        averages = sma(prices, 20)
        result: List[Signal] = []
        holding = False
        for index in range(len(prices)):
            if index < 20 or averages[index] is None:
                result.append(Signal(0.0, "AI特征预热"))
                continue
            features = self._features(prices, index, rsi_values, averages)
            probability = float(self.model.predict_probability(features))
            if not 0 <= probability <= 1 or not math.isfinite(probability):
                raise ValueError("model probability must be finite and in [0, 1]")
            if probability >= self.entry_probability:
                holding = True
                action = "模型达到开仓阈值"
            elif probability <= self.exit_probability:
                holding = False
                action = "模型达到退出阈值"
            else:
                action = "模型处于滞回区间，保持状态"
            result.append(
                Signal(
                    1.0 if holding else 0.0,
                    "AI上涨概率 %.1f%%；%s" % (probability * 100, action),
                )
            )
        return result
