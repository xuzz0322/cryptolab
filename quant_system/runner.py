"""Safe strategy runner connecting model signals to an ExchangeAdapter."""

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

from .exchange import ExchangeAdapter
from .models import Bar, Signal
from .order_models import Order, OrderType, Side


@dataclass
class TradingRunnerConfig:
    dry_run: bool = True
    warmup_bars: int = 60
    max_history_bars: int = 1000
    max_position_weight: float = 0.50
    max_order_notional: float = 1_000.0
    min_rebalance_notional: float = 10.0
    max_orders_per_day: int = 3
    max_consecutive_errors: int = 3

    def validate(self) -> None:
        if self.warmup_bars < 2 or self.max_history_bars < self.warmup_bars:
            raise ValueError("invalid runner history configuration")
        if not 0 <= self.max_position_weight <= 1:
            raise ValueError("max_position_weight must be in [0, 1]")
        if self.max_order_notional <= 0 or self.min_rebalance_notional < 0:
            raise ValueError("invalid order notional limits")
        if self.max_orders_per_day < 1 or self.max_consecutive_errors < 1:
            raise ValueError("runner limits must be positive")


@dataclass
class RunnerDecision:
    signal: Optional[Signal]
    order: Optional[Order]
    submitted: bool
    reason: str


class TradingRunner:
    """Runs any Strategy, including AIModelStrategy, through one guarded path.

    The runner defaults to dry_run=True. Models can only return target weights;
    they never receive the adapter, credentials, order manager or account.
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        strategy,
        symbol: str = "BTC/USDT",
        config: Optional[TradingRunnerConfig] = None,
    ):
        self.adapter = adapter
        self.strategy = strategy
        self.symbol = symbol
        self.config = config or TradingRunnerConfig()
        self.config.validate()
        if not self.config.dry_run and getattr(strategy, "name", "") == "ai_signal":
            raise PermissionError("AI策略自动交易必须通过GovernedTradingSession和已批准版本启动")
        if not self.config.dry_run and adapter.venue != "PAPER":
            raise PermissionError("非Paper自动交易必须迁移到GovernedTradingSession")
        self.bars: List[Bar] = []
        self.orders_per_day: Dict[date, int] = {}
        self.consecutive_errors = 0
        self.stopped = False

    async def _account_quantities(self, base: str, quote: str):
        balances = {item.currency: item for item in await self.adapter.get_balances()}
        base_balance = balances.get(base)
        quote_balance = balances.get(quote)
        base_total = base_balance.total if base_balance else 0.0
        base_available = base_balance.free if base_balance else 0.0
        quote_total = quote_balance.total if quote_balance else 0.0
        return base_total, base_available, quote_total

    async def on_bar(self, bar: Bar) -> RunnerDecision:
        if self.stopped:
            return RunnerDecision(None, None, False, "运行器已停止")
        self.bars.append(bar)
        self.bars = self.bars[-self.config.max_history_bars :]
        if len(self.bars) < self.config.warmup_bars:
            return RunnerDecision(None, None, False, "策略预热 %d/%d" % (len(self.bars), self.config.warmup_bars))
        signals = self.strategy.generate_signals(self.bars)
        signal = signals[-1]
        instruments = await self.adapter.get_instruments()
        instrument = next((item for item in instruments if item.symbol == self.symbol), None)
        if instrument is None:
            return RunnerDecision(signal, None, False, "交易所没有该币对主数据")
        base_total, base_available, quote_total = await self._account_quantities(
            instrument.base_currency, instrument.quote_currency
        )
        equity = quote_total + base_total * bar.close
        target_weight = min(max(signal.target_weight, 0.0), self.config.max_position_weight)
        target_quantity = instrument.normalize_quantity(target_weight * equity / bar.close)
        delta = instrument.normalize_quantity(abs(target_quantity - base_total))
        if delta * bar.close < max(
            self.config.min_rebalance_notional, float(instrument.minimum_notional)
        ):
            return RunnerDecision(signal, None, False, "仓位偏差低于再平衡门槛")
        side = Side.BUY if target_quantity > base_total else Side.SELL
        if side == Side.SELL:
            delta = min(delta, base_available)
        max_quantity = instrument.normalize_quantity(self.config.max_order_notional / bar.close)
        delta = instrument.normalize_quantity(min(delta, max_quantity))
        if delta <= 0 or delta * bar.close < float(instrument.minimum_notional):
            return RunnerDecision(signal, None, False, "订单低于交易所最小数量或名义金额")
        count = self.orders_per_day.get(bar.date, 0)
        if count >= self.config.max_orders_per_day:
            return RunnerDecision(signal, None, False, "触发单日订单频率限制")
        order = Order(
            symbol=self.symbol,
            side=side,
            quantity=delta,
            trading_date=bar.date,
            order_type=OrderType.MARKET,
            reason="%s；Runner目标仓位 %.1f%%" % (signal.reason, target_weight * 100),
        )
        if self.config.dry_run:
            return RunnerDecision(signal, order, False, "Dry Run：订单未提交")
        submitted = await self.adapter.submit_order(order)
        self.orders_per_day[bar.date] = count + 1
        return RunnerDecision(signal, submitted, True, "订单已提交至 %s" % self.adapter.venue)

    async def stop(self, activate_kill_switch: bool = False) -> None:
        self.stopped = True
        if activate_kill_switch and hasattr(self.adapter, "set_kill_switch"):
            await self.adapter.set_kill_switch(True)

    async def run(self) -> None:
        async for bar in self.adapter.stream_bars():
            try:
                await self.on_bar(bar)
                self.consecutive_errors = 0
            except Exception:
                self.consecutive_errors += 1
                if self.consecutive_errors >= self.config.max_consecutive_errors:
                    await self.stop(activate_kill_switch=True)
                    raise
