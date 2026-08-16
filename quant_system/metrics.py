import math
from typing import Dict, List, Sequence

from .models import EquityPoint, Trade


def _safe_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = sum(values) / len(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / (len(values) - 1))


def calculate_metrics(
    equity_curve: Sequence[EquityPoint], trades: Sequence[Trade], risk_free_rate: float = 0.02
) -> Dict[str, float]:
    if len(equity_curve) < 2:
        return {}
    equities = [point.equity for point in equity_curve]
    daily_returns = [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities))]
    total_return = equities[-1] / equities[0] - 1
    years = max((equity_curve[-1].date - equity_curve[0].date).days / 365.25, 1 / 252)
    annual_return = (equities[-1] / equities[0]) ** (1 / years) - 1
    volatility = _safe_std(daily_returns) * math.sqrt(252)
    daily_rf = risk_free_rate / 252
    excess = [value - daily_rf for value in daily_returns]
    excess_std = _safe_std(excess)
    sharpe = (sum(excess) / len(excess)) / excess_std * math.sqrt(252) if excess_std else 0.0
    downside = [min(value - daily_rf, 0.0) for value in daily_returns]
    downside_std = math.sqrt(sum(value * value for value in downside) / len(downside)) if downside else 0
    sortino = (sum(excess) / len(excess)) / downside_std * math.sqrt(252) if downside_std else 0.0
    max_drawdown = min((point.drawdown for point in equity_curve), default=0.0)
    sell_trades = [trade for trade in trades if trade.side == "SELL"]
    wins = [trade for trade in sell_trades if trade.realized_pnl > 0]
    gross_profit = sum(max(trade.realized_pnl, 0) for trade in sell_trades)
    gross_loss = -sum(min(trade.realized_pnl, 0) for trade in sell_trades)
    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_volatility": volatility,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "max_drawdown": max_drawdown,
        "calmar_ratio": annual_return / abs(max_drawdown) if max_drawdown else 0.0,
        "trade_count": float(len(trades)),
        "closed_trades": float(len(sell_trades)),
        "win_rate": len(wins) / len(sell_trades) if sell_trades else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else (gross_profit > 0) * 999.0,
        "final_equity": equities[-1],
    }

