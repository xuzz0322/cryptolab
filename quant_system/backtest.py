from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Sequence

from .metrics import calculate_metrics
from .models import BacktestConfig, Bar, EquityPoint, Trade
from .broker import SimulatedBroker
from .instruments import Instrument, get_instrument
from .market_rules import CryptoSpotRules, MarketRules
from .oms import InMemoryOrderRepository, OrderManager
from .order_models import Fill, Order, OrderType, Side
from .portfolio import Portfolio
from .risk import RiskManager
from .strategies import Strategy


@dataclass
class BacktestResult:
    config: BacktestConfig
    strategy: str
    metrics: Dict[str, float]
    equity_curve: List[EquityPoint]
    trades: List[Trade]
    benchmark_curve: List[float]
    orders: List[Order]
    fills: List[Fill]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": asdict(self.config),
            "strategy": self.strategy,
            "metrics": self.metrics,
            "equity_curve": [point.to_dict() for point in self.equity_curve],
            "trades": [trade.to_dict() for trade in self.trades],
            "benchmark_curve": self.benchmark_curve,
            "orders": [order.to_dict() for order in self.orders],
            "fills": [fill.to_dict() for fill in self.fills],
        }


class BacktestEngine:
    """Daily-bar, long-only engine. Signals at close are filled at next open."""

    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()
        self.config.validate()
        self.risk_manager = RiskManager(self.config)

    def run(
        self,
        bars: Sequence[Bar],
        strategy: Strategy,
        symbol: str = "BTC/USDT",
        market_rules: MarketRules = None,
        instrument: Instrument = None,
    ) -> BacktestResult:
        if len(bars) < 2:
            raise ValueError("backtest needs at least two bars")
        if any(bars[i].date >= bars[i + 1].date for i in range(len(bars) - 1)):
            raise ValueError("bars must be strictly chronological")
        signals = strategy.generate_signals(bars)
        if len(signals) != len(bars):
            raise ValueError("strategy signal count must equal bar count")

        if instrument is None:
            try:
                instrument = get_instrument(symbol)
            except ValueError:
                from decimal import Decimal

                instrument = Instrument(
                    symbol,
                    "SIMULATED",
                    symbol,
                    "CASH",
                    Decimal("0.000001"),
                    Decimal(str(self.config.lot_size)),
                    Decimal(str(self.config.lot_size)),
                    Decimal(str(self.config.minimum_notional)),
                )
        rules = market_rules or CryptoSpotRules(instrument)
        portfolio = Portfolio(self.config.initial_cash, settlement_days=rules.settlement_days)
        repository = InMemoryOrderRepository()
        order_manager = OrderManager(repository)
        broker = SimulatedBroker(self.config, order_manager, rules, instrument)
        trades: List[Trade] = []
        curve: List[EquityPoint] = []

        # Day 0 establishes the starting NAV. Day i executes yesterday's signal at open.
        portfolio.start_day(bars[0].date, {symbol: bars[0].close})
        curve.append(EquityPoint(bars[0].date, portfolio.equity, portfolio.cash, 0.0, 0.0))
        for index in range(1, len(bars)):
            bar = bars[index]
            portfolio.start_day(bar.date, {symbol: bar.open})
            order_manager.cancel_active(symbol, "新交易日信号更新")
            position = portfolio.position(symbol)
            previous_signal = signals[index - 1]
            risk = self.risk_manager.evaluate(
                previous_signal, bar.open, position.average_cost or None, position.quantity > 0
            )
            pre_trade_equity = portfolio.equity
            target_value = pre_trade_equity * risk.target_weight
            desired_quantity = rules.normalize_quantity(
                instrument, target_value / (bar.open * (1 + self.config.slippage_rate))
            )
            delta = desired_quantity - position.quantity

            if delta:
                side = Side.BUY if delta > 0 else Side.SELL
                order_quantity = abs(delta)
                if side == Side.SELL:
                    order_quantity = min(order_quantity, position.available_quantity)
                if order_quantity > 0:
                    order = order_manager.create(
                        Order(
                            symbol=symbol,
                            side=side,
                            quantity=order_quantity,
                            trading_date=bar.date,
                            order_type=OrderType.MARKET,
                            reason=risk.reason,
                        )
                    )
                    decision = self.risk_manager.check_order(order, portfolio, bar.open)
                    market_decision = rules.validate_order(order, bar.open)
                    if not market_decision.allowed:
                        order_manager.reject(order, market_decision.reason)
                    elif decision.target_weight == 0:
                        order_manager.reject(order, decision.reason)
                    else:
                        average_cost_before = position.average_cost
                        order_manager.approve_and_submit(order)
                        execution = broker.execute(order, bar, portfolio, bars[index - 1].close)
                        if execution.fill:
                            fill = execution.fill
                            realized = (
                                fill.quantity * (fill.price - average_cost_before) - fill.commission
                                if fill.side == Side.SELL
                                else 0.0
                            )
                            trades.append(
                                Trade(
                                    bar.date,
                                    fill.side.value,
                                    fill.quantity,
                                    fill.price,
                                    fill.commission,
                                    order.reason,
                                    realized,
                                )
                            )

            portfolio.mark({symbol: bar.close})
            equity = portfolio.equity
            position = portfolio.position(symbol)
            curve.append(
                EquityPoint(bar.date, equity, portfolio.cash, position.market_value, portfolio.drawdown)
            )

        benchmark = [self.config.initial_cash * bar.close / bars[0].close for bar in bars]
        metrics = calculate_metrics(curve, trades)
        metrics["benchmark_return"] = benchmark[-1] / benchmark[0] - 1
        metrics["excess_return"] = metrics["total_return"] - metrics["benchmark_return"]
        return BacktestResult(
            self.config,
            strategy.name,
            metrics,
            curve,
            trades,
            benchmark,
            repository.list_orders(),
            repository.list_fills(),
        )
