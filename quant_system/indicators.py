import math
from typing import List, Optional, Sequence


MaybeFloat = Optional[float]


def sma(values: Sequence[float], window: int) -> List[MaybeFloat]:
    if window <= 0:
        raise ValueError("window must be positive")
    result: List[MaybeFloat] = []
    rolling = 0.0
    for index, value in enumerate(values):
        rolling += value
        if index >= window:
            rolling -= values[index - window]
        result.append(rolling / window if index >= window - 1 else None)
    return result


def ema(values: Sequence[float], window: int) -> List[MaybeFloat]:
    if window <= 0:
        raise ValueError("window must be positive")
    if not values:
        return []
    alpha = 2.0 / (window + 1)
    current = values[0]
    result: List[MaybeFloat] = []
    for index, value in enumerate(values):
        current = value if index == 0 else alpha * value + (1 - alpha) * current
        result.append(current if index >= window - 1 else None)
    return result


def rolling_std(values: Sequence[float], window: int) -> List[MaybeFloat]:
    averages = sma(values, window)
    result: List[MaybeFloat] = []
    for index, average in enumerate(averages):
        if average is None:
            result.append(None)
            continue
        sample = values[index - window + 1 : index + 1]
        variance = sum((value - average) ** 2 for value in sample) / window
        result.append(math.sqrt(variance))
    return result


def rsi(values: Sequence[float], window: int = 14) -> List[MaybeFloat]:
    if window <= 0:
        raise ValueError("window must be positive")
    result: List[MaybeFloat] = [None] * len(values)
    if len(values) <= window:
        return result
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    avg_gain = sum(gains[:window]) / window
    avg_loss = sum(losses[:window]) / window
    result[window] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for index in range(window + 1, len(values)):
        avg_gain = (avg_gain * (window - 1) + gains[index - 1]) / window
        avg_loss = (avg_loss * (window - 1) + losses[index - 1]) / window
        result[index] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return result


def returns(values: Sequence[float]) -> List[float]:
    result = [0.0]
    result.extend(values[i] / values[i - 1] - 1 for i in range(1, len(values)))
    return result


def momentum(values: Sequence[float], window: int) -> List[MaybeFloat]:
    """Trailing simple return using only the current and past prices."""
    if window <= 0:
        raise ValueError("window must be positive")
    result: List[MaybeFloat] = []
    for index, value in enumerate(values):
        if index < window:
            result.append(None)
        else:
            result.append(value / values[index - window] - 1)
    return result


def realized_volatility(
    values: Sequence[float], window: int, periods_per_year: int = 365
) -> List[MaybeFloat]:
    """Annualized trailing volatility from exactly ``window`` past returns."""
    if window < 2:
        raise ValueError("window must be at least 2")
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    daily_returns = returns(values)
    result: List[MaybeFloat] = []
    for index in range(len(values)):
        if index < window:
            result.append(None)
            continue
        sample = daily_returns[index - window + 1 : index + 1]
        average = sum(sample) / len(sample)
        variance = sum((value - average) ** 2 for value in sample) / (len(sample) - 1)
        result.append(math.sqrt(variance * periods_per_year))
    return result
