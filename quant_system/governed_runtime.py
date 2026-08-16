"""Bind an approved strategy version to exactly one rollout environment."""

from typing import Optional

from .adapters import BinanceSpotTestnetAdapter, OKXSpotAdapter, PaperExchangeAdapter
from .deployment import DeploymentPolicy, RolloutStage
from .event_engine import EventDrivenTradingEngine
from .exchange import ExchangeAdapter
from .instruments import Instrument
from .risk_service import IndependentRiskService
from .strategies import create_strategy
from .strategy_governance import StrategyGovernanceService


class GovernedTradingSession:
    """The supported entry point for Paper, Testnet and capped Live trading."""

    def __init__(
        self,
        governance: StrategyGovernanceService,
        version_id: str,
        stage: RolloutStage,
        adapter: ExchangeAdapter,
        instrument: Instrument,
        risk_service: Optional[IndependentRiskService] = None,
        policy: Optional[DeploymentPolicy] = None,
        alert_manager=None,
        evidence_store=None,
    ):
        self.governance = governance
        self.version_id = version_id
        self.stage = stage
        self.adapter = adapter
        self.instrument = instrument
        self.risk_service = risk_service
        self.policy = policy or governance.gate.policy
        self.alert_manager = alert_manager
        self.evidence_store = evidence_store
        self.engine: Optional[EventDrivenTradingEngine] = None

    def _validate_binding(self) -> None:
        record = self.governance.get(self.version_id)
        if record.stage != self.stage:
            raise PermissionError(
                "策略版本处于 %s，不能在 %s 环境运行" % (record.stage.value, self.stage.value)
            )
        if self.stage == RolloutStage.PAPER and not isinstance(self.adapter, PaperExchangeAdapter):
            raise PermissionError("PAPER阶段只能绑定PaperExchangeAdapter")
        if self.stage == RolloutStage.TESTNET:
            if not isinstance(self.adapter, (BinanceSpotTestnetAdapter, OKXSpotAdapter)):
                raise PermissionError("TESTNET阶段必须绑定受支持的模拟盘Adapter")
            if isinstance(self.adapter, BinanceSpotTestnetAdapter) and self.adapter.base_url == self.adapter.production_url:
                raise PermissionError("TESTNET阶段禁止Binance生产地址")
            if isinstance(self.adapter, OKXSpotAdapter) and not self.adapter.demo:
                raise PermissionError("TESTNET阶段必须启用OKX Demo Trading")
        if self.stage == RolloutStage.LIVE:
            if not isinstance(self.adapter, (BinanceSpotTestnetAdapter, OKXSpotAdapter)):
                raise PermissionError("LIVE阶段必须绑定经过审核的真实Adapter")
            if isinstance(self.adapter, BinanceSpotTestnetAdapter) and self.adapter.base_url != self.adapter.production_url:
                raise PermissionError("LIVE阶段地址与部署声明不一致")
            if isinstance(self.adapter, OKXSpotAdapter) and self.adapter.demo:
                raise PermissionError("LIVE阶段禁止OKX Demo Trading")
            if not self.adapter.allow_live or not self.adapter.trading_enabled:
                raise PermissionError("LIVE阶段仍需Adapter双重交易开关")

    async def start(self) -> EventDrivenTradingEngine:
        self._validate_binding()
        if not self.adapter.connected:
            await self.adapter.connect()
        if self.stage == RolloutStage.LIVE:
            snapshot = await self.adapter.reconcile()
            quote = next(
                (item for item in snapshot.balances if item.currency == self.instrument.quote_currency), None
            )
            quote_capital = quote.total if quote else 0.0
            if quote_capital > self.policy.maximum_live_capital:
                raise PermissionError(
                    "实盘子账户资金 %.2f 超过小资金上限 %.2f"
                    % (quote_capital, self.policy.maximum_live_capital)
                )
            if snapshot.positions:
                raise PermissionError("小资金实盘启动前要求使用无遗留持仓的独立子账户")
        record = self.governance.get(self.version_id)
        strategy = create_strategy(record.proposal.strategy_name, record.proposal.parameters)
        self.engine = EventDrivenTradingEngine(
            self.adapter,
            strategy,
            self.instrument,
            strategy_version=self.version_id,
            risk_service=self.risk_service,
            governed=True,
            rollout_stage=self.stage,
            alert_manager=self.alert_manager,
            evidence_store=self.evidence_store,
        )
        return self.engine

    async def stop(self, activate_kill_switch: bool = False) -> None:
        if self.engine and activate_kill_switch:
            self.engine.risk.set_kill_switch(True)
        if activate_kill_switch and hasattr(self.adapter, "set_kill_switch"):
            await self.adapter.set_kill_switch(True)
        await self.adapter.disconnect()
