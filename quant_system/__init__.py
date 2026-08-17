"""Educational quantitative research and backtesting toolkit."""

from importlib import import_module

from .backtest import BacktestEngine
from .data import generate_market_data, load_csv
from .models import BacktestConfig, Bar
from .providers import BinanceSpotProvider, CoinbaseSpotProvider, PublicCryptoProvider, CachedMarketDataProvider, CsvMarketDataProvider, MarketDataProvider, TushareProvider
from .instruments import INSTRUMENTS, Instrument, get_instrument
from .market_rules import AShareRules, CryptoSpotRules, MarketRules
from .broker import SimulatedBroker
from .oms import InMemoryOrderRepository, OrderManager, SqliteOrderRepository
from .order_models import Fill, Order, OrderStatus, OrderType, Side
from .portfolio import Portfolio, Position
from .exchange import (
    AccountSnapshot,
    Balance,
    ExchangeAdapter,
    ExchangePosition,
    ExecutionRouter,
    ExchangeUserEvent,
    UserEventKind,
)
from .adapters import BinanceSpotTestnetAdapter, OKXSpotAdapter, PaperExchangeAdapter
from .ai_strategy import AIModelStrategy, FeatureVector, LinearProbabilityModel, ProbabilityModel
from .runner import RunnerDecision, TradingRunner, TradingRunnerConfig
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
from .event_engine import EventDrivenTradingEngine, PortfolioConstructionConfig
from .risk_service import IndependentRiskService, RiskContext, RiskLimits
from .ledger import LedgerReconciliationService, LedgerState
from .deployment import DeploymentEvidence, DeploymentGate, DeploymentPolicy, RolloutStage
from .strategy_governance import (
    StrategyGovernanceService,
    StrategyProposal,
    StrategyVersionRecord,
    ValidationCriteria,
    ValidationReport,
)
from .governed_runtime import GovernedTradingSession
from .alerts import Alert, AlertManager, AlertSeverity, MemoryAlertSink, WebhookAlertSink
from .evidence import RuntimeEvidenceStore
from .rate_limit import AsyncExchangeRateLimiter, RateLimitPolicy
from .strategies import STRATEGIES, create_strategy

_RESEARCH_EXPORTS = {
    "CandidateSpec",
    "ResearchPolicy",
    "ResearchReport",
    "StrategyResearchPipeline",
    "annual_sharpe",
    "deflated_sharpe_ratio",
    "minimum_track_record_length",
    "probability_of_backtest_overfitting",
}


def __getattr__(name):
    # Avoid pre-importing quant_system.research when it is executed with
    # ``python -m quant_system.research``; eager import triggers a runpy warning.
    if name in _RESEARCH_EXPORTS:
        value = getattr(import_module(".research", __name__), name)
        globals()[name] = value
        return value
    raise AttributeError("module %r has no attribute %r" % (__name__, name))

__all__ = [
    "BacktestEngine",
    "BacktestConfig",
    "Bar",
    "MarketDataProvider",
    "CsvMarketDataProvider",
    "CachedMarketDataProvider",
    "TushareProvider",
    "BinanceSpotProvider",
    "CoinbaseSpotProvider",
    "PublicCryptoProvider",
    "Instrument",
    "INSTRUMENTS",
    "get_instrument",
    "MarketRules",
    "CryptoSpotRules",
    "AShareRules",
    "Order",
    "Fill",
    "Side",
    "OrderType",
    "OrderStatus",
    "OrderManager",
    "InMemoryOrderRepository",
    "SqliteOrderRepository",
    "SimulatedBroker",
    "Portfolio",
    "Position",
    "ExchangeAdapter",
    "ExecutionRouter",
    "Balance",
    "ExchangePosition",
    "AccountSnapshot",
    "ExchangeUserEvent",
    "UserEventKind",
    "PaperExchangeAdapter",
    "BinanceSpotTestnetAdapter",
    "OKXSpotAdapter",
    "AIModelStrategy",
    "FeatureVector",
    "ProbabilityModel",
    "LinearProbabilityModel",
    "TradingRunner",
    "TradingRunnerConfig",
    "RunnerDecision",
    "AsyncEventBus",
    "EventKind",
    "MarketEvent",
    "SignalEvent",
    "OrderIntentEvent",
    "RiskDecisionEvent",
    "OrderUpdateEvent",
    "FillEvent",
    "ReconciliationEvent",
    "ControlEvent",
    "EventDrivenTradingEngine",
    "PortfolioConstructionConfig",
    "IndependentRiskService",
    "RiskLimits",
    "RiskContext",
    "LedgerReconciliationService",
    "LedgerState",
    "RolloutStage",
    "DeploymentEvidence",
    "DeploymentPolicy",
    "DeploymentGate",
    "StrategyProposal",
    "StrategyVersionRecord",
    "ValidationCriteria",
    "ValidationReport",
    "StrategyGovernanceService",
    "GovernedTradingSession",
    "Alert",
    "AlertSeverity",
    "AlertManager",
    "MemoryAlertSink",
    "WebhookAlertSink",
    "RuntimeEvidenceStore",
    "AsyncExchangeRateLimiter",
    "RateLimitPolicy",
    "CandidateSpec",
    "ResearchPolicy",
    "ResearchReport",
    "StrategyResearchPipeline",
    "annual_sharpe",
    "deflated_sharpe_ratio",
    "minimum_track_record_length",
    "probability_of_backtest_overfitting",
    "STRATEGIES",
    "create_strategy",
    "generate_market_data",
    "load_csv",
]
