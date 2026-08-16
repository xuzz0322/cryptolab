"""Event-driven trading application composed from independent services."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from .events import (
    AsyncEventBus,
    ControlEvent,
    EventKind,
    FillEvent,
    MarketEvent,
    OrderIntentEvent,
    OrderUpdateEvent,
    ReconciliationEvent,
    RiskDecisionEvent,
    SignalEvent,
)
from .exchange import ExchangeAdapter
from .exchange import UserEventKind
from .alerts import Alert, AlertManager, AlertSeverity
from .deployment import RolloutStage
from .evidence import RuntimeEvidenceStore
from .instruments import Instrument
from .ledger import LedgerReconciliationService
from .oms import InMemoryOrderRepository, OrderRepository
from .order_models import Order, OrderType, Side
from .risk_service import IndependentRiskService, RiskContext


@dataclass
class PortfolioConstructionConfig:
    target_weight_cap: float = 0.20
    max_slice_notional: float = 500.0
    min_rebalance_notional: float = 10.0


class StrategyEventService:
    def __init__(self, bus: AsyncEventBus, strategy, version: str, warmup_bars: int = 60):
        self.bus = bus
        self.strategy = strategy
        self.version = version
        self.warmup_bars = warmup_bars
        self.bars: List = []

    async def on_market(self, event: MarketEvent) -> None:
        self.bars.append(event.bar)
        self.bars = self.bars[-1000:]
        if len(self.bars) < self.warmup_bars:
            return
        signal = self.strategy.generate_signals(self.bars)[-1]
        await self.bus.publish(
            SignalEvent(
                symbol=event.symbol,
                target_weight=signal.target_weight,
                strategy_name=self.strategy.name,
                strategy_version=self.version,
                reason=signal.reason,
                trading_date=event.bar.date,
                causation_id=event.event_id,
            )
        )


class PortfolioConstructionService:
    def __init__(
        self,
        bus: AsyncEventBus,
        ledger: LedgerReconciliationService,
        instrument: Instrument,
        config: Optional[PortfolioConstructionConfig] = None,
    ):
        self.bus, self.ledger, self.instrument = bus, ledger, instrument
        self.config = config or PortfolioConstructionConfig()
        self.last_price = 0.0

    def update_price(self, price: float) -> None:
        self.last_price = price

    async def on_signal(self, event: SignalEvent) -> None:
        state = self.ledger.state
        if self.last_price <= 0 or state.equity <= 0:
            return
        target_weight = min(max(event.target_weight, 0.0), self.config.target_weight_cap)
        target_quantity = self.instrument.normalize_quantity(
            target_weight * state.equity / self.last_price
        )
        raw_delta = target_quantity - state.base_quantity
        side = Side.BUY if raw_delta > 0 else Side.SELL
        quantity = self.instrument.normalize_quantity(abs(raw_delta))
        quantity = self.instrument.normalize_quantity(
            min(quantity, self.config.max_slice_notional / self.last_price)
        )
        if side == Side.SELL:
            quantity = min(quantity, state.available_quantity)
        minimum = max(self.config.min_rebalance_notional, float(self.instrument.minimum_notional))
        if quantity <= 0 or quantity * self.last_price < minimum:
            return
        await self.bus.publish(
            OrderIntentEvent(
                symbol=event.symbol,
                side=side,
                quantity=quantity,
                reference_price=self.last_price,
                strategy_name=event.strategy_name,
                strategy_version=event.strategy_version,
                reason=event.reason,
                trading_date=event.trading_date,
                causation_id=event.event_id,
            )
        )


class RiskEventService:
    def __init__(self, bus: AsyncEventBus, risk: IndependentRiskService, ledger: LedgerReconciliationService):
        self.bus, self.risk, self.ledger = bus, risk, ledger

    async def on_intent(self, event: OrderIntentEvent) -> None:
        state = self.ledger.state
        age = max(0.0, (datetime.now(timezone.utc) - event.created_at).total_seconds())
        result = self.risk.evaluate(
            event,
            RiskContext(
                equity=state.equity,
                cash=state.cash,
                current_quantity=state.base_quantity,
                available_quantity=state.available_quantity,
                current_position_value=state.position_value,
                daily_return=state.daily_return,
                drawdown=state.drawdown,
                orders_today=state.orders_today,
                market_age_seconds=age,
            ),
        )
        await self.bus.publish(
            RiskDecisionEvent(event, result.approved, result.reasons, result.approved_quantity)
        )


class ExecutionEventService:
    def __init__(self, bus: AsyncEventBus, adapter: ExchangeAdapter):
        self.bus, self.adapter = bus, adapter

    async def on_risk_decision(self, event: RiskDecisionEvent) -> None:
        if not event.approved:
            return
        intent = event.intent
        order = Order(
            symbol=intent.symbol,
            side=intent.side,
            quantity=event.approved_quantity,
            trading_date=intent.trading_date,
            order_type=OrderType.MARKET,
            reason="%s；%s" % (intent.reason, "；".join(event.reasons)),
            client_order_id=intent.intent_id,
        )
        result = await self.adapter.submit_order(order)
        await self.bus.publish(
            OrderUpdateEvent(result, self.adapter.venue, causation_id=event.event_id)
        )


class EventDrivenTradingEngine:
    """Primary trading path. Strategies and models cannot reach execution directly."""

    def __init__(
        self,
        adapter: ExchangeAdapter,
        strategy,
        instrument: Instrument,
        strategy_version: str,
        risk_service: Optional[IndependentRiskService] = None,
        bus: Optional[AsyncEventBus] = None,
        oms_repository: Optional[OrderRepository] = None,
        warmup_bars: int = 60,
        governed: bool = False,
        alert_manager: Optional[AlertManager] = None,
        evidence_store: Optional[RuntimeEvidenceStore] = None,
        rollout_stage: RolloutStage = RolloutStage.PAPER,
    ):
        if adapter.venue != "PAPER" and not governed:
            raise PermissionError("非Paper事件交易必须通过GovernedTradingSession启动")
        if getattr(strategy, "name", "") == "ai_signal" and not governed:
            raise PermissionError("AI策略只能使用已批准的策略版本启动")
        self.adapter = adapter
        self.instrument = instrument
        self.bus = bus or AsyncEventBus()
        self.risk = risk_service or IndependentRiskService()
        self.ledger = LedgerReconciliationService()
        self.oms_repository = oms_repository or InMemoryOrderRepository()
        self.strategy_service = StrategyEventService(
            self.bus, strategy, strategy_version, warmup_bars
        )
        self.portfolio_service = PortfolioConstructionService(
            self.bus, self.ledger, instrument,
            PortfolioConstructionConfig(
                target_weight_cap=self.risk.limits.max_position_weight,
                max_slice_notional=self.risk.limits.max_order_notional,
            ),
        )
        self.risk_service = RiskEventService(self.bus, self.risk, self.ledger)
        self.execution_service = ExecutionEventService(self.bus, adapter)
        self.latest_price = 0.0
        self.latest_date = None
        self.halted = False
        self.governed = governed
        self.alert_manager = alert_manager or AlertManager()
        self.evidence_store = evidence_store
        self.rollout_stage = rollout_stage
        self._order_update_keys = set()
        self._fill_ids = set()
        self._wire()

    def _wire(self) -> None:
        self.bus.subscribe(EventKind.MARKET, self._on_market_ledger)
        self.bus.subscribe(EventKind.MARKET, self.strategy_service.on_market)
        self.bus.subscribe(EventKind.SIGNAL, self.portfolio_service.on_signal)
        self.bus.subscribe(EventKind.ORDER_INTENT, self.risk_service.on_intent)
        self.bus.subscribe(EventKind.RISK_DECISION, self.execution_service.on_risk_decision)
        self.bus.subscribe(EventKind.ORDER_UPDATE, self._on_order_update)
        self.bus.subscribe(EventKind.FILL, self._on_fill)
        self.bus.subscribe(EventKind.RECONCILIATION, self._on_reconciliation)
        self.bus.subscribe(EventKind.CONTROL, self._on_control)

    async def _on_market_ledger(self, event: MarketEvent) -> None:
        self.latest_price, self.latest_date = event.bar.close, event.bar.date
        self.portfolio_service.update_price(event.bar.close)
        reconciliation = await self.ledger.reconcile(
            self.adapter, self.instrument, event.bar.close, event.bar.date
        )
        if self.evidence_store:
            self.evidence_store.record_heartbeat(
                self.rollout_stage,
                event.bar.date,
                self.ledger.state.equity,
                self.ledger.state.drawdown,
            )
        await self.bus.publish(reconciliation)

    async def _on_order_update(self, event: OrderUpdateEvent) -> None:
        key = (
            event.order.order_id,
            event.order.status.value,
            round(event.order.filled_quantity, 12),
        )
        if key in self._order_update_keys:
            return
        self._order_update_keys.add(key)
        self.oms_repository.save_order(event.order)
        self.ledger.record_order_update(event)
        if self.evidence_store:
            self.evidence_store.record_order(
                self.rollout_stage, event.order.trading_date, event.order.order_id
            )
        reconciliation = await self.ledger.reconcile(
            self.adapter, self.instrument, self.latest_price, self.latest_date
        )
        await self.bus.publish(reconciliation)

    async def _on_fill(self, event: FillEvent) -> None:
        if event.fill.fill_id in self._fill_ids:
            return
        self._fill_ids.add(event.fill.fill_id)
        self.oms_repository.save_fill(event.fill)

    async def _on_reconciliation(self, event: ReconciliationEvent) -> None:
        if event.discrepancies:
            if self.evidence_store and self.latest_date:
                self.evidence_store.record_reconciliation_error(
                    self.rollout_stage, self.latest_date, len(event.discrepancies)
                )
            await self.alert_manager.emit(
                Alert(
                    "账本对账差异",
                    "系统已准备Fail Closed",
                    AlertSeverity.CRITICAL,
                    "LedgerReconciliationService",
                    {
                        "venue": self.adapter.venue,
                        "discrepancies": event.discrepancies,
                        "count": len(event.discrepancies),
                    },
                ),
                dedupe_key="ledger-reconciliation-difference",
            )
            await self.bus.publish(
                ControlEvent("HALT", "；".join(event.discrepancies))
            )

    async def _on_control(self, event: ControlEvent) -> None:
        if event.command.upper() == "HALT":
            self.halted = True
            self.risk.set_kill_switch(True)
            if hasattr(self.adapter, "set_kill_switch"):
                await self.adapter.set_kill_switch(True)
            await self.alert_manager.emit(
                Alert(
                    "交易系统已停机",
                    event.reason,
                    AlertSeverity.CRITICAL,
                    "EventDrivenTradingEngine",
                    {"venue": self.adapter.venue, "error": event.reason},
                ),
                dedupe_key="trading-engine-halted",
            )

    async def ingest_bar(self, bar, update_paper_adapter: bool = False) -> int:
        if self.halted:
            raise RuntimeError("event-driven engine is halted")
        if update_paper_adapter and hasattr(self.adapter, "publish_bar"):
            await self.adapter.publish_bar(bar, self.instrument.symbol)
        await self.bus.publish(MarketEvent(self.instrument.symbol, bar))
        return await self.bus.drain()

    async def _run_market_stream(self) -> None:
        async for bar in self.adapter.stream_bars():
            await self.ingest_bar(bar)

    async def _run_user_stream(self) -> None:
        async for update in self.adapter.stream_user_events():
            if update.order:
                await self.bus.publish(OrderUpdateEvent(update.order, update.venue))
            if update.fill:
                await self.bus.publish(FillEvent(update.fill, update.venue))
            if update.kind == UserEventKind.BALANCE and self.latest_price and self.latest_date:
                reconciliation = await self.ledger.reconcile(
                    self.adapter, self.instrument, self.latest_price, self.latest_date
                )
                await self.bus.publish(reconciliation)
            await self.bus.drain()

    async def run(self) -> None:
        tasks = {
            asyncio.create_task(self._run_market_stream()),
            asyncio.create_task(self._run_user_stream()),
        }
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exception = task.exception()
                if exception:
                    if self.evidence_store and self.latest_date:
                        self.evidence_store.record_runtime_error(
                            self.rollout_stage, self.latest_date, str(exception)
                        )
                    await self.alert_manager.emit(
                        Alert(
                            "交易运行任务异常",
                            str(exception),
                            AlertSeverity.CRITICAL,
                            "EventDrivenTradingEngine",
                            {"venue": self.adapter.venue, "error": str(exception)},
                        )
                    )
                    raise exception
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
