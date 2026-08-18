import json
import asyncio
from datetime import timedelta
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

from quant_system.api import run_backtest, serve
from quant_system.backtest import BacktestEngine
from quant_system.data import generate_market_data, load_csv
from quant_system.indicators import momentum, realized_volatility, rsi, sma
from quant_system.models import BacktestConfig
from quant_system.broker import SimulatedBroker
from quant_system.models import Bar
from quant_system.oms import InMemoryOrderRepository, OrderManager, SqliteOrderRepository
from quant_system.order_models import Fill, Order, OrderStatus, OrderType, Side
from quant_system.portfolio import Portfolio
from quant_system.risk import RiskManager
from quant_system.adapters import BinanceSpotTestnetAdapter, OKXSpotAdapter, PaperExchangeAdapter
from quant_system.exchange import ExecutionRouter, TradingDisabledError
from quant_system.ai_strategy import AIModelStrategy, FeatureVector, ProbabilityModel
from quant_system.models import Signal
from quant_system.runner import TradingRunner, TradingRunnerConfig
from quant_system.deployment import DeploymentEvidence, DeploymentGate, DeploymentPolicy, RolloutStage
from quant_system.event_engine import EventDrivenTradingEngine
from quant_system.events import AsyncEventBus, EventKind, MarketEvent
from quant_system.risk_service import IndependentRiskService, RiskContext, RiskLimits
from quant_system.strategy_governance import (
    StrategyGovernanceService,
    StrategyProposal,
    ValidationCriteria,
)
from quant_system.governed_runtime import GovernedTradingSession
from quant_system.alerts import Alert, AlertManager, AlertSeverity, MemoryAlertSink, WebhookAlertSink
from quant_system.evidence import RuntimeEvidenceStore
from quant_system.exchange import UserEventKind
from quant_system.rate_limit import AsyncExchangeRateLimiter, RateLimitPolicy
from quant_system.research import (
    CandidateSpec,
    ResearchPolicy,
    StrategyResearchPipeline,
    deflated_sharpe_ratio,
    minimum_track_record_length,
    probability_of_backtest_overfitting,
)
from quant_system.providers import CachedMarketDataProvider, MarketDataProvider, TushareProvider
from quant_system.providers import BinanceSpotProvider, CoinbaseSpotProvider, MarketDataError, PublicCryptoProvider
from quant_system.instruments import get_instrument
from quant_system.market_rules import CryptoSpotRules
from quant_system.strategies import MovingAverageCrossStrategy, RegimeTrendStrategy, create_strategy


class IndicatorTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(sma([1, 2, 3, 4], 3), [None, None, 2.0, 3.0])

    def test_rsi_in_strong_uptrend(self):
        values = rsi(list(range(1, 30)), 14)
        self.assertEqual(values[-1], 100.0)

    def test_momentum_and_crypto_realized_volatility_are_causal(self):
        values = [100, 101, 103, 102, 106]
        values_momentum = momentum(values, 2)
        volatility = realized_volatility(values, 3, periods_per_year=365)
        self.assertEqual(values_momentum[:2], [None, None])
        self.assertAlmostEqual(values_momentum[2], 0.03)
        self.assertEqual(volatility[:3], [None, None, None])
        self.assertGreater(volatility[-1], 0)


class RegimeTrendStrategyTests(unittest.TestCase):
    @staticmethod
    def bars(prices):
        start = date(2025, 1, 1)
        return [
            Bar(start + timedelta(days=index), price, price * 1.01, price * 0.99, price, 1000)
            for index, price in enumerate(prices)
        ]

    def test_enters_holds_with_hysteresis_and_exits(self):
        strategy = RegimeTrendStrategy(
            long_window=5,
            momentum_window=3,
            volatility_window=3,
            entry_momentum=0.02,
            exit_momentum=-0.05,
            entry_trend_buffer=0.01,
            exit_trend_buffer=0.02,
            max_annualized_volatility=5.0,
            target_weight=0.7,
        )
        rising = [100, 102, 104, 106, 108, 110, 112, 113]
        held = rising + [112.5]
        held_signals = strategy.generate_signals(self.bars(held))
        self.assertEqual(held_signals[-1].target_weight, 0.7)
        self.assertIn("滞回", held_signals[-1].reason)
        exited = strategy.generate_signals(self.bars(held + [80]))
        self.assertEqual(exited[-1].target_weight, 0.0)
        self.assertIn("退出", exited[-1].reason)

    def test_warmup_volatility_filter_and_no_future_leakage(self):
        strategy = RegimeTrendStrategy(
            long_window=5,
            momentum_window=3,
            volatility_window=3,
            entry_momentum=0.01,
            max_annualized_volatility=0.05,
        )
        prices = [100, 120, 105, 130, 112, 140, 120, 150, 125, 155]
        signals = strategy.generate_signals(self.bars(prices))
        self.assertTrue(all(signal.target_weight == 0 for signal in signals[:5]))
        self.assertEqual(signals[-1].target_weight, 0.0)

        normal = RegimeTrendStrategy(
            long_window=5, momentum_window=3, volatility_window=3,
            entry_momentum=0.01, max_annualized_volatility=5.0,
        )
        original = normal.generate_signals(self.bars([100 + index for index in range(20)]))
        changed = normal.generate_signals(self.bars([100 + index for index in range(19)] + [500]))
        self.assertEqual(original[:-1], changed[:-1])

    def test_rejects_invalid_parameters_and_factory_registers_strategy(self):
        with self.assertRaises(ValueError):
            RegimeTrendStrategy(long_window=10, momentum_window=20)
        with self.assertRaises(ValueError):
            RegimeTrendStrategy(entry_momentum=-0.1, exit_momentum=0.1)
        with self.assertRaises(ValueError):
            RegimeTrendStrategy(target_weight=1.1)
        self.assertIsInstance(create_strategy("regime_trend"), RegimeTrendStrategy)


class DataTests(unittest.TestCase):
    def test_synthetic_data_is_reproducible_and_valid(self):
        first = generate_market_data(120, seed=7)
        second = generate_market_data(120, seed=7)
        self.assertEqual(first, second)
        self.assertTrue(all(bar.low <= min(bar.open, bar.close) for bar in first))
        self.assertTrue(all(bar.high >= max(bar.open, bar.close) for bar in first))

    def test_csv_loader(self):
        content = "date,open,high,low,close,volume\n2025-01-02,10,11,9,10.5,1000\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            path.write_text(content, encoding="utf-8")
            bars = load_csv(path)
        self.assertEqual(bars[0].close, 10.5)

    def test_tushare_provider_normalizes_and_forward_adjusts(self):
        responses = {
            "daily": {
                "code": 0,
                "data": {
                    "fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol"],
                    "items": [
                        ["000001.SZ", "20250103", 20, 22, 19, 21, 200],
                        ["000001.SZ", "20250102", 10, 11, 9, 10.5, 100],
                    ],
                },
            },
            "adj_factor": {
                "code": 0,
                "data": {
                    "fields": ["trade_date", "adj_factor"],
                    "items": [["20250103", 2.0], ["20250102", 1.0]],
                },
            },
        }

        def transport(url, payload, timeout):
            self.assertEqual(url, "https://api.tushare.pro")
            self.assertEqual(timeout, 15.0)
            self.assertNotIn("demo-token", json.dumps(payload.get("params")))
            return responses[payload["api_name"]]

        provider = TushareProvider(token="demo-token", transport=transport)
        bars = provider.get_daily_bars(
            "000001.SZ",
            date(2025, 1, 1),
            date(2025, 1, 5),
        )
        self.assertEqual([bar.date.isoformat() for bar in bars], ["2025-01-02", "2025-01-03"])
        self.assertEqual(bars[0].close, 5.25)
        self.assertEqual(bars[1].close, 21.0)
        self.assertEqual(bars[0].volume, 10_000)

    def test_cached_provider_avoids_second_upstream_call(self):
        class FakeProvider(MarketDataProvider):
            calls = 0

            def get_daily_bars(self, symbol, start, end):
                self.calls += 1
                return generate_market_data(5)

        start = date(2025, 1, 1)
        end = date(2025, 1, 31)
        with tempfile.TemporaryDirectory() as directory:
            upstream = FakeProvider()
            provider = CachedMarketDataProvider(upstream, Path(directory))
            first = provider.get_daily_bars("000001.SZ", start, end)
            second = provider.get_daily_bars("000001.SZ", start, end)
        self.assertEqual(upstream.calls, 1)
        self.assertEqual(first, second)

    def test_binance_provider_normalizes_public_klines(self):
        def transport(url, params, timeout):
            self.assertIn("/api/v3/klines", url)
            self.assertEqual(params["symbol"], "BTCUSDT")
            self.assertEqual(params["interval"], "1d")
            return [[1735776000000, "95000", "97000", "94000", "96500", "123.456", 0, 0, 0, 0, 0, 0]]

        bars = BinanceSpotProvider(transport=transport).get_daily_bars(
            "BTC/USDT", date(2025, 1, 2), date(2025, 1, 2)
        )
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].date, date(2025, 1, 2))
        self.assertEqual(bars[0].close, 96_500)
        self.assertEqual(bars[0].volume, 123.456)

    def test_coinbase_provider_and_public_fallback(self):
        def transport(url, params, timeout):
            self.assertIn("BTC-USDT/candles", url)
            self.assertEqual(params["granularity"], 86400)
            return [[1735776000, "94000", "97000", "95000", "96500", "123.456"]]

        coinbase = CoinbaseSpotProvider(transport=transport)
        bars = coinbase.get_daily_bars("BTC/USDT", date(2025, 1, 2), date(2025, 1, 2))
        self.assertEqual(bars[0].close, 96_500)

        class BrokenProvider(MarketDataProvider):
            def get_daily_bars(self, symbol, start, end):
                raise MarketDataError("region blocked")

        fallback = PublicCryptoProvider([BrokenProvider(), coinbase])
        self.assertEqual(fallback.get_daily_bars("BTC/USDT", date(2025, 1, 2), date(2025, 1, 2)), bars)


class BacktestTests(unittest.TestCase):
    def test_backtest_is_deterministic_and_accounts_for_equity(self):
        bars = generate_market_data(300, seed=42)
        config = BacktestConfig(max_position_weight=0.8)
        strategy = MovingAverageCrossStrategy(10, 30)
        first = BacktestEngine(config).run(bars, strategy)
        second = BacktestEngine(config).run(bars, strategy)
        self.assertEqual(first.metrics, second.metrics)
        self.assertEqual(len(first.equity_curve), len(bars))
        self.assertTrue(first.trades)
        self.assertEqual(len(first.fills), len(first.trades))
        self.assertTrue(all(order.status in {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED} for order in first.orders))
        for point in first.equity_curve:
            self.assertAlmostEqual(point.equity, point.cash + point.position_value, places=6)
            self.assertLessEqual(point.drawdown, 0)

    def test_strategy_factory_rejects_unknown_name(self):
        with self.assertRaises(ValueError):
            create_strategy("made_up")

    def test_api_service_function(self):
        result = run_backtest({"strategy": "rsi_reversion", "days": 180, "seed": 9})
        self.assertEqual(result["strategy"], "rsi_reversion")
        self.assertEqual(len(result["bars"]), 180)
        self.assertIn("sharpe_ratio", result["metrics"])
        self.assertIn("orders", result)
        self.assertIn("fills", result)


class ResearchValidationTests(unittest.TestCase):
    def test_overfit_statistics_are_deterministic_and_degrade_without_enough_trials(self):
        selected = [0.002, -0.001, 0.003, 0.001, -0.0005] * 40
        trial_sharpes = [0.01, 0.02, 0.03]
        dsr, benchmark = deflated_sharpe_ratio(selected, trial_sharpes)
        self.assertGreaterEqual(dsr, 0)
        self.assertLessEqual(dsr, 1)
        self.assertGreaterEqual(benchmark, 0)
        self.assertIsNotNone(minimum_track_record_length(selected))
        self.assertIsNone(probability_of_backtest_overfitting([[0.01, 0.02, 0.03]] * 40))

    def test_pipeline_exports_trials_and_binds_exact_governance_fingerprint(self):
        bars = generate_market_data(260, seed=17)
        candidates = [
            CandidateSpec("ma_cross", {"short_window": 5, "long_window": 20}, "ma-5-20"),
            CandidateSpec("rsi_reversion", {"window": 14, "buy_below": 30, "sell_above": 70}, "rsi"),
            CandidateSpec("bollinger", {"window": 20, "std_multiplier": 2.0}, "bollinger"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            report = StrategyResearchPipeline(
                ResearchPolicy(walk_forward_window=30, cost_multipliers=(1.0, 2.0))
            ).run("BTC/USDT", {"BTC/USDT": bars}, candidates, Path(directory))
            self.assertFalse(report.passed)
            self.assertIsNone(report.pbo)
            self.assertTrue(any("PBO" in reason for reason in report.failures))
            self.assertTrue((Path(directory) / "selected_returns.csv").exists())
            self.assertTrue((Path(directory) / "trials_matrix.csv").exists())
            self.assertTrue((Path(directory) / "research_report.json").exists())

            governance = StrategyGovernanceService(
                criteria=ValidationCriteria(require_research_report=True),
                storage_path=Path(directory) / "governance.db",
            )
            record = governance.propose(StrategyProposal(
                report.selected_strategy_name,
                report.selected_parameters,
                "pipeline-selected candidate",
                proposed_by="HUMAN_OPERATOR",
            ))
            validation = governance.record_research_validation(record.proposal.version_id, report)
            self.assertFalse(validation.passed)
            with self.assertRaises(ValueError):
                governance.promote(record.proposal.version_id, RolloutStage.VALIDATED)
            tampered = report.to_dict()
            tampered["selected_fingerprint"] = "0" * 64
            with self.assertRaises(ValueError):
                governance.record_research_validation(record.proposal.version_id, tampered)
            governance.close()


class OrderManagementTests(unittest.TestCase):
    def test_order_state_machine_partial_fill_and_cancel(self):
        repository = InMemoryOrderRepository()
        manager = OrderManager(repository)
        order = manager.create(Order("000001.SZ", Side.BUY, 100, date(2025, 1, 2)))
        manager.approve_and_submit(order)
        self.assertEqual(order.status, OrderStatus.ACCEPTED)
        manager.record_fill(order, Fill(order.order_id, order.symbol, order.side, 40, 10.0, 0.12, order.trading_date))
        self.assertEqual(order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(order.remaining_quantity, 60)
        manager.cancel(order)
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        with self.assertRaises(ValueError):
            manager.transition(order, OrderStatus.FILLED)

    def test_duplicate_client_order_id_is_idempotent(self):
        manager = OrderManager()
        first = Order("000001.SZ", Side.BUY, 100, date(2025, 1, 2), client_order_id="signal-1")
        second = Order("000001.SZ", Side.BUY, 200, date(2025, 1, 2), client_order_id="signal-1")
        self.assertIs(manager.create(first), manager.create(second))

    def test_sqlite_repository_recovers_orders_and_fills(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oms.db"
            repository = SqliteOrderRepository(path)
            manager = OrderManager(repository)
            order = manager.create(Order("600519.SH", Side.BUY, 100, date(2025, 1, 2)))
            manager.approve_and_submit(order)
            fill = Fill(order.order_id, order.symbol, order.side, 100, 1500.0, 45.0, order.trading_date)
            manager.record_fill(order, fill)
            repository.close()
            recovered = SqliteOrderRepository(path)
            self.assertEqual(recovered.get_order(order.order_id).status, OrderStatus.FILLED)
            self.assertEqual(recovered.list_fills()[0], fill)
            recovered.close()

    def test_crypto_broker_partial_fill_and_t_zero(self):
        config = BacktestConfig(initial_cash=100_000, max_volume_participation=0.1, lot_size=0.00001)
        manager = OrderManager()
        instrument = get_instrument("BTC/USDT")
        rules = CryptoSpotRules(instrument)
        broker = SimulatedBroker(config, manager, rules, instrument)
        portfolio = Portfolio(10_000)
        portfolio.start_day(date(2025, 1, 2), {"BTC/USDT": 100.0})
        order = manager.create(Order("BTC/USDT", Side.BUY, 1.0, date(2025, 1, 2)))
        manager.approve_and_submit(order)
        result = broker.execute(order, Bar(date(2025, 1, 2), 100, 102, 98, 101, 5), portfolio)
        self.assertEqual(result.fill.quantity, 0.5)
        self.assertEqual(order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(portfolio.position("BTC/USDT").available_quantity, 0.5)

    def test_portfolio_can_still_apply_a_share_t_plus_one(self):
        portfolio = Portfolio(10_000, settlement_days=1)
        portfolio.start_day(date(2025, 1, 2), {"000001.SZ": 10.0})
        portfolio.apply_fill(Fill("order", "000001.SZ", Side.BUY, 100, 10, 3, date(2025, 1, 2)))
        self.assertEqual(portfolio.position("000001.SZ").available_quantity, 0)
        portfolio.start_day(date(2025, 1, 3), {"000001.SZ": 10.1})
        self.assertEqual(portfolio.position("000001.SZ").available_quantity, 100)

    def test_pretrade_risk_rejects_invalid_lot_and_t_plus_one_sell(self):
        config = BacktestConfig(initial_cash=10_000, lot_size=100)
        portfolio = Portfolio(10_000)
        portfolio.start_day(date(2025, 1, 2), {"000001.SZ": 10.0})
        risk = RiskManager(config)
        invalid_lot = Order("000001.SZ", Side.BUY, 50, date(2025, 1, 2))
        self.assertEqual(risk.check_order(invalid_lot, portfolio, 10).target_weight, 0.0)
        unavailable_sell = Order("000001.SZ", Side.SELL, 100, date(2025, 1, 2))
        self.assertIn("可用持仓", risk.check_order(unavailable_sell, portfolio, 10).reason)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.url = serve("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health_and_dashboard(self):
        with urlopen(self.url + "/api/health") as response:
            self.assertEqual(json.load(response), {"status": "ok"})
        with urlopen(self.url + "/") as response:
            dashboard = response.read().decode("utf-8")
            self.assertIn("CryptoLab", dashboard)
            self.assertIn("BTC/USDT", dashboard)
            self.assertIn("T+0", dashboard)

    def test_backtest_endpoint(self):
        request = Request(
            self.url + "/api/backtest",
            data=json.dumps({"strategy": "bollinger", "days": 150}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            result = json.load(response)
        self.assertEqual(result["strategy"], "bollinger")


class ExchangeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_paper_adapter_full_order_account_and_kill_switch_flow(self):
        adapter = PaperExchangeAdapter(
            "BTC/USDT", config=BacktestConfig(initial_cash=10_000, max_position_weight=0.95)
        )
        await adapter.connect()
        await adapter.publish_bar(Bar(date(2025, 1, 2), 100, 102, 98, 101, 100))
        buy = Order("BTC/USDT", Side.BUY, 0.5, date(2025, 1, 2))
        result = await adapter.submit_order(buy)
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual((await adapter.get_positions())[0].quantity, 0.5)
        self.assertEqual(len(await adapter.get_fills("BTC/USDT")), 1)
        snapshot = await adapter.reconcile()
        self.assertEqual(snapshot.venue, "PAPER")
        self.assertTrue(any(balance.currency == "USDT" for balance in snapshot.balances))

        # Crypto spot is T+0: the same quantity can be sold on the same date.
        sell = Order("BTC/USDT", Side.SELL, 0.5, date(2025, 1, 2))
        self.assertEqual((await adapter.submit_order(sell)).status, OrderStatus.FILLED)
        self.assertEqual(await adapter.get_positions(), [])

        await adapter.set_kill_switch(True)
        with self.assertRaises(TradingDisabledError):
            await adapter.submit_order(Order("BTC/USDT", Side.BUY, 0.1, date(2025, 1, 2)))
        await adapter.disconnect()

    async def test_execution_router_dispatches_to_registered_venue(self):
        adapter = PaperExchangeAdapter("BTC/USDT", initial_cash=10_000)
        router = ExecutionRouter()
        router.register(adapter)
        await adapter.connect()
        await adapter.publish_bar(Bar(date(2025, 1, 2), 100, 101, 99, 100, 100))
        order = Order("BTC/USDT", Side.BUY, 0.1, date(2025, 1, 2))
        self.assertEqual((await router.submit_order("paper", order)).status, OrderStatus.FILLED)
        with self.assertRaises(ValueError):
            router.register(adapter)

    async def test_binance_testnet_signature_status_and_trading_guard(self):
        calls = []

        def transport(method, url, headers, data, timeout):
            calls.append((method, url, headers, data, timeout))
            if url.endswith("/api/v3/time"):
                return {"serverTime": 1_735_776_000_000}
            if "/api/v3/order" in url and method == "POST":
                body = data.decode("utf-8")
                self.assertIn("signature=", body)
                self.assertIn("newClientOrderId=", body)
                self.assertEqual(headers["X-MBX-APIKEY"], "test-key")
                return {
                    "symbol": "BTCUSDT", "orderId": 123, "clientOrderId": "client",
                    "transactTime": 1_735_776_000_000, "price": "0", "origQty": "0.01",
                    "executedQty": "0.01", "cummulativeQuoteQty": "950", "status": "FILLED",
                    "type": "MARKET", "side": "BUY"
                }
            if "/api/v3/account" in url and method == "GET":
                self.assertIn("signature=", url)
                return {"balances": [
                    {"asset": "USDT", "free": "9050", "locked": "0"},
                    {"asset": "BTC", "free": "0.01", "locked": "0"},
                ]}
            if "/api/v3/openOrders" in url and method == "GET":
                return []
            raise AssertionError("unexpected request %s %s" % (method, url))

        guarded = BinanceSpotTestnetAdapter(
            api_key="test-key", api_secret="test-secret", transport=transport
        )
        await guarded.connect()
        with self.assertRaises(TradingDisabledError):
            await guarded.submit_order(Order("BTC/USDT", Side.BUY, 0.01, date(2025, 1, 2)))

        adapter = BinanceSpotTestnetAdapter(
            api_key="test-key", api_secret="test-secret", trading_enabled=True, transport=transport
        )
        await adapter.connect()
        order = Order(
            "BTC/USDT", Side.BUY, 0.01, date(2025, 1, 2), client_order_id="client"
        )
        result = await adapter.submit_order(order)
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(result.exchange_order_id, "123")
        self.assertEqual(result.average_fill_price, 95_000)
        snapshot = await adapter.reconcile()
        self.assertEqual(snapshot.venue, "BINANCE")
        self.assertEqual(snapshot.positions[0].symbol, "BTC/USDT")
        self.assertEqual(snapshot.open_orders, [])
        self.assertTrue(calls)

    async def test_binance_production_requires_explicit_live_authorization(self):
        with self.assertRaises(ValueError):
            BinanceSpotTestnetAdapter(base_url="https://api.binance.com")

    async def test_okx_demo_rest_signature_trading_guard_and_reconciliation(self):
        calls = []

        def transport(method, url, headers, data, timeout):
            calls.append((method, url, headers, data))
            self.assertEqual(headers.get("x-simulated-trading"), "1")
            if url.endswith("/api/v5/public/time"):
                return {"code": "0", "data": [{"ts": "1735776000000"}]}
            self.assertEqual(headers.get("OK-ACCESS-KEY"), "okx-key")
            self.assertIn("OK-ACCESS-SIGN", headers)
            self.assertEqual(headers.get("OK-ACCESS-PASSPHRASE"), "passphrase")
            if url.endswith("/api/v5/trade/order") and method == "POST":
                body = json.loads(data.decode("utf-8"))
                self.assertEqual(body["instId"], "BTC-USDT")
                self.assertEqual(body["tgtCcy"], "base_ccy")
                return {"code": "0", "data": [{"ordId": "9001", "clOrdId": body["clOrdId"], "sCode": "0"}]}
            if "/api/v5/account/balance" in url:
                return {"code": "0", "data": [{"details": [
                    {"ccy": "USDT", "cashBal": "9050", "availBal": "9050", "frozenBal": "0"},
                    {"ccy": "BTC", "cashBal": "0.01", "availBal": "0.01", "frozenBal": "0"},
                ]}]}
            if "/api/v5/trade/orders-pending" in url:
                return {"code": "0", "data": []}
            raise AssertionError("unexpected request %s %s" % (method, url))

        guarded = OKXSpotAdapter(
            api_key="okx-key", api_secret="okx-secret", passphrase="passphrase", transport=transport
        )
        await guarded.connect()
        with self.assertRaises(TradingDisabledError):
            await guarded.submit_order(Order("BTC/USDT", Side.BUY, 0.01, date(2025, 1, 2)))

        adapter = OKXSpotAdapter(
            api_key="okx-key", api_secret="okx-secret", passphrase="passphrase",
            trading_enabled=True, transport=transport,
        )
        await adapter.connect()
        result = await adapter.submit_order(
            Order("BTC/USDT", Side.BUY, 0.01, date(2025, 1, 2), client_order_id="okx-client")
        )
        self.assertEqual(result.status, OrderStatus.SUBMITTED)
        self.assertEqual(result.exchange_order_id, "9001")
        snapshot = await adapter.reconcile()
        self.assertEqual(snapshot.venue, "OKX")
        self.assertEqual(snapshot.positions[0].symbol, "BTC/USDT")
        self.assertTrue(calls)
        with self.assertRaises(ValueError):
            OKXSpotAdapter(demo=False)

    async def test_okx_private_stream_reconnects_logs_in_and_emits_fill(self):
        sockets = []

        class FakeSocket:
            def __init__(self, messages=None, failure=None):
                self.messages = list(messages or [])
                self.failure = failure
                self.sent = []
                self.closed = False

            async def send(self, message):
                self.sent.append(message)

            async def recv(self):
                if self.failure:
                    failure, self.failure = self.failure, None
                    raise failure
                if self.messages:
                    return self.messages.pop(0)
                await asyncio.Future()

            async def close(self):
                self.closed = True

        login = json.dumps({"event": "login", "code": "0"})
        sub_orders = json.dumps({"event": "subscribe", "arg": {"channel": "orders"}})
        sub_account = json.dumps({"event": "subscribe", "arg": {"channel": "account"}})
        fill = json.dumps({
            "arg": {"channel": "orders", "instType": "SPOT"},
            "data": [{
                "instId": "BTC-USDT", "ordId": "9001", "clOrdId": "okx-stream-client",
                "side": "buy", "ordType": "market", "sz": "0.01", "state": "filled",
                "accFillSz": "0.01", "avgPx": "95000", "fillSz": "0.01",
                "fillPx": "95000", "tradeId": "trade-77", "fee": "-0.00001",
                "uTime": "1735776000000",
            }],
        })

        async def connector(url):
            self.assertIn("wspap.okx.com", url)
            if not sockets:
                socket = FakeSocket(failure=ConnectionError("simulated disconnect"))
            else:
                socket = FakeSocket([login, sub_orders, sub_account, fill])
            sockets.append(socket)
            return socket

        def transport(method, url, headers, data, timeout):
            if url.endswith("/api/v5/public/time"):
                return {"code": "0", "data": [{"ts": "1735776000000"}]}
            raise AssertionError("unexpected request %s %s" % (method, url))

        alerts = MemoryAlertSink()
        adapter = OKXSpotAdapter(
            api_key="key", api_secret="secret", passphrase="pass", transport=transport,
            websocket_connector=connector,
            alert_manager=AlertManager([alerts], cooldown_seconds=0),
            reconnect_max_seconds=0,
        )
        order = Order("BTC/USDT", Side.BUY, 0.01, date(2025, 1, 2), client_order_id="okx-stream-client")
        adapter._orders[order.order_id] = order
        await adapter.connect()
        stream = adapter.stream_user_events()
        event = await stream.__anext__()
        self.assertEqual(event.kind, UserEventKind.TRADE)
        self.assertEqual(event.order.status, OrderStatus.FILLED)
        self.assertEqual(event.fill.fill_id, "trade-77")
        self.assertEqual(event.fill.price, 95_000)
        self.assertGreaterEqual(len(sockets), 2)
        self.assertTrue(alerts.alerts)
        sent = "\n".join(sockets[1].sent)
        self.assertIn('"op":"login"', sent)
        self.assertIn('"channel":"orders"', sent)
        self.assertNotIn("secret", sent)
        await stream.aclose()
        await adapter.disconnect()

    async def test_binance_user_stream_reconnects_and_emits_real_time_fill(self):
        sockets = []
        put_keepalives = []

        class FakeSocket:
            def __init__(self, messages=None, failure=None, delay=0):
                self.messages = list(messages or [])
                self.failure = failure
                self.delay = delay
                self.closed = False

            async def recv(self):
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.failure:
                    failure, self.failure = self.failure, None
                    raise failure
                if self.messages:
                    return self.messages.pop(0)
                await asyncio.Future()

            async def close(self):
                self.closed = True

        execution_report = json.dumps({
            "e": "executionReport", "E": 1735776000000, "s": "BTCUSDT",
            "c": "stream-client", "S": "BUY", "o": "MARKET", "q": "0.01",
            "p": "0", "i": 321, "X": "FILLED", "z": "0.01", "Z": "950",
            "l": "0.01", "L": "95000", "n": "0.00001", "t": 77,
        })

        async def connector(url):
            self.assertIn("listen-key-test", url)
            if not sockets:
                socket = FakeSocket(failure=ConnectionError("simulated disconnect"))
            else:
                socket = FakeSocket([execution_report], delay=0.01)
            sockets.append(socket)
            return socket

        def transport(method, url, headers, data, timeout):
            if url.endswith("/api/v3/time"):
                return {"serverTime": 1735776000000}
            if "/api/v3/userDataStream" in url and method == "POST":
                return {"listenKey": "listen-key-test"}
            if "/api/v3/userDataStream" in url and method == "PUT":
                put_keepalives.append(url)
                return {}
            if "/api/v3/userDataStream" in url and method == "DELETE":
                return {}
            raise AssertionError("unexpected request %s %s" % (method, url))

        alerts = MemoryAlertSink()
        adapter = BinanceSpotTestnetAdapter(
            api_key="key", api_secret="secret", transport=transport,
            websocket_connector=connector,
            alert_manager=AlertManager([alerts], cooldown_seconds=0),
            listen_key_keepalive_seconds=0.002,
            reconnect_max_seconds=0,
        )
        order = Order("BTC/USDT", Side.BUY, 0.01, date(2025, 1, 2), client_order_id="stream-client")
        adapter._orders[order.order_id] = order
        await adapter.connect()
        stream = adapter.stream_user_events()
        event = await stream.__anext__()
        self.assertEqual(event.kind, UserEventKind.TRADE)
        self.assertEqual(event.order.status, OrderStatus.FILLED)
        self.assertEqual(event.fill.fill_id, "77")
        self.assertEqual(event.fill.price, 95_000)
        self.assertGreaterEqual(len(sockets), 2)
        self.assertTrue(alerts.alerts)
        self.assertTrue(put_keepalives)
        await stream.aclose()
        await adapter.disconnect()


class OperationsSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limiter_tracks_weight_and_order_windows(self):
        limiter = AsyncExchangeRateLimiter(
            RateLimitPolicy(request_weight_per_minute=2, orders_per_10_seconds=1, orders_per_day=2)
        )
        self.assertTrue(await limiter.try_acquire(weight=1, is_order=True))
        self.assertFalse(await limiter.try_acquire(weight=1, is_order=True))
        self.assertTrue(await limiter.try_acquire(weight=1, is_order=False))
        self.assertFalse(await limiter.try_acquire(weight=1, is_order=False))
        snapshot = await limiter.snapshot()
        self.assertEqual(snapshot["request_weight_1m"], 2)
        self.assertEqual(snapshot["orders_10s"], 1)

    async def test_external_webhook_alert_is_https_deduplicated_and_secret_safe(self):
        bodies = []

        def transport(url, body, timeout):
            bodies.append(json.loads(body.decode("utf-8")))

        sink = WebhookAlertSink("https://alerts.example.test/hook", transport=transport)
        manager = AlertManager([sink], cooldown_seconds=60)
        alert = Alert(
            "用户流异常", "自动重连", AlertSeverity.CRITICAL, "test",
            {"venue": "BINANCE", "error": "disconnect", "api_secret": "must-not-leak"},
        )
        self.assertTrue(await manager.emit(alert, "same"))
        self.assertFalse(await manager.emit(alert, "same"))
        self.assertEqual(len(bodies), 1)
        self.assertNotIn("api_secret", bodies[0]["details"])
        with self.assertRaises(ValueError):
            WebhookAlertSink("http://insecure.example.test/hook")

    async def test_runtime_evidence_is_durable_verified_and_gate_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeEvidenceStore(Path(directory) / "evidence.db")
            start = date(2025, 1, 1)
            for offset in range(14):
                store.record_heartbeat(RolloutStage.PAPER, start + timedelta(days=offset), 10_000, -0.02)
            for index in range(20):
                store.record_order(RolloutStage.PAPER, start + timedelta(days=index % 14), "paper-%d" % index)
            # Repeated state updates for one exchange order are not extra samples.
            store.record_order(RolloutStage.PAPER, start, "paper-0")
            evidence = store.build_evidence(RolloutStage.PAPER)
            self.assertTrue(evidence.verified)
            self.assertEqual(len(evidence.evidence_digest), 64)
            self.assertEqual(evidence.paper_days, 14)
            self.assertEqual(evidence.paper_orders, 20)
            decision = DeploymentGate().evaluate(RolloutStage.PAPER, RolloutStage.TESTNET, evidence)
            self.assertTrue(decision.approved, decision.reasons)
            store.close()


class AIAndRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_ai_model_strategy_is_a_standard_target_weight_strategy(self):
        class ConstantModel(ProbabilityModel):
            def __init__(self, probability):
                self.probability = probability

            def predict_probability(self, features: FeatureVector) -> float:
                self.asserted_feature = features
                return self.probability

        bars = generate_market_data(80)
        bullish = AIModelStrategy(model=ConstantModel(0.9)).generate_signals(bars)
        bearish = AIModelStrategy(model=ConstantModel(0.1)).generate_signals(bars)
        self.assertEqual(bullish[-1].target_weight, 1.0)
        self.assertEqual(bearish[-1].target_weight, 0.0)
        self.assertIn("AI上涨概率", bullish[-1].reason)
        self.assertEqual(create_strategy("ai_signal").name, "ai_signal")

    async def test_runner_defaults_to_dry_run_then_submits_through_paper_adapter(self):
        class AlwaysLongStrategy:
            name = "always_long"

            def generate_signals(self, bars):
                return [Signal(1.0, "测试目标多头") for _ in bars]

        adapter = PaperExchangeAdapter("BTC/USDT", initial_cash=10_000)
        await adapter.connect()
        dry_runner = TradingRunner(
            adapter,
            AlwaysLongStrategy(),
            config=TradingRunnerConfig(dry_run=True, warmup_bars=2, max_order_notional=1_000),
        )
        first = Bar(date(2025, 1, 2), 100, 101, 99, 100, 1_000)
        second = Bar(date(2025, 1, 3), 100, 102, 99, 101, 1_000)
        await adapter.publish_bar(first)
        self.assertIn("预热", (await dry_runner.on_bar(first)).reason)
        await adapter.publish_bar(second)
        dry_decision = await dry_runner.on_bar(second)
        self.assertFalse(dry_decision.submitted)
        self.assertIn("Dry Run", dry_decision.reason)
        self.assertEqual(await adapter.get_fills(), [])

        live_runner = TradingRunner(
            adapter,
            AlwaysLongStrategy(),
            config=TradingRunnerConfig(
                dry_run=False, warmup_bars=2, max_order_notional=1_000, max_orders_per_day=1
            ),
        )
        await live_runner.on_bar(first)
        submitted = await live_runner.on_bar(second)
        self.assertTrue(submitted.submitted)
        self.assertEqual(submitted.order.status, OrderStatus.FILLED)
        limited = await live_runner.on_bar(second)
        self.assertIn("频率", limited.reason)
        self.assertEqual(len(await adapter.get_fills()), 1)


class EventArchitectureTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_bus_preserves_fifo_and_records_dead_letters(self):
        bus = AsyncEventBus()
        received = []

        async def handler(event):
            received.append(event.symbol)

        bus.subscribe(EventKind.MARKET, handler)
        await bus.publish(MarketEvent("BTC/USDT", Bar(date(2025, 1, 2), 100, 101, 99, 100, 10)))
        await bus.publish(MarketEvent("ETH/USDT", Bar(date(2025, 1, 2), 10, 11, 9, 10, 10)))
        self.assertEqual(await bus.drain(), 2)
        self.assertEqual(received, ["BTC/USDT", "ETH/USDT"])

    async def test_event_driven_engine_decouples_strategy_risk_execution_and_ledger(self):
        class AlwaysLongStrategy:
            name = "always_long"

            def generate_signals(self, bars):
                return [Signal(1.0, "事件策略目标多头") for _ in bars]

        adapter = PaperExchangeAdapter("BTC/USDT", initial_cash=10_000)
        await adapter.connect()
        risk = IndependentRiskService(
            RiskLimits(max_position_weight=0.20, max_order_notional=500, max_orders_per_day=3)
        )
        engine = EventDrivenTradingEngine(
            adapter,
            AlwaysLongStrategy(),
            get_instrument("BTC/USDT"),
            strategy_version="test-v1",
            risk_service=risk,
            warmup_bars=2,
        )
        await engine.ingest_bar(
            Bar(date(2025, 1, 2), 100, 101, 99, 100, 1000), update_paper_adapter=True
        )
        await engine.ingest_bar(
            Bar(date(2025, 1, 3), 100, 102, 99, 101, 1000), update_paper_adapter=True
        )
        kinds = [event.kind for event in engine.bus.event_log]
        self.assertIn(EventKind.SIGNAL, kinds)
        self.assertIn(EventKind.ORDER_INTENT, kinds)
        self.assertIn(EventKind.RISK_DECISION, kinds)
        self.assertIn(EventKind.ORDER_UPDATE, kinds)
        self.assertIn(EventKind.RECONCILIATION, kinds)
        self.assertEqual(len(engine.oms_repository.list_orders()), 1)
        self.assertTrue((await adapter.get_positions())[0].quantity > 0)
        self.assertFalse(engine.halted)

    async def test_reconciliation_difference_fails_closed(self):
        class FlatStrategy:
            name = "flat"

            def generate_signals(self, bars):
                return [Signal(0.0, "空仓") for _ in bars]

        adapter = PaperExchangeAdapter("BTC/USDT", initial_cash=10_000)
        await adapter.connect()
        engine = EventDrivenTradingEngine(
            adapter, FlatStrategy(), get_instrument("BTC/USDT"), "flat-v1", warmup_bars=2
        )
        engine.ledger.expected_active_order_ids.add("ghost-order")
        await engine.ingest_bar(
            Bar(date(2025, 1, 2), 100, 101, 99, 100, 1000), update_paper_adapter=True
        )
        self.assertTrue(engine.halted)
        self.assertTrue(engine.risk.kill_switch)

    def test_independent_risk_is_fail_closed(self):
        from quant_system.events import OrderIntentEvent

        service = IndependentRiskService(
            RiskLimits(max_position_weight=0.20, max_order_notional=100)
        )
        intent = OrderIntentEvent(
            "BTC/USDT", Side.BUY, 2, 100, "strategy", "v1", "test", date(2025, 1, 2)
        )
        context = RiskContext(1000, 1000, 0, 0, 0, 0, 0, 0, 0)
        result = service.evaluate(intent, context)
        self.assertFalse(result.approved)
        self.assertTrue(any("名义金额" in reason for reason in result.reasons))


class StrategyGovernanceTests(unittest.TestCase):
    def test_ai_can_propose_validate_but_only_human_promotes_in_sequence(self):
        governance = StrategyGovernanceService(
            criteria=ValidationCriteria(
                minimum_bars=120, minimum_trades=0, minimum_sharpe=-999, maximum_drawdown=1
            )
        )
        proposal = StrategyProposal(
            "ai_signal",
            {"entry_probability": 0.55, "exit_probability": 0.45},
            "AI生成的候选版本，仅供验证",
        )
        record = governance.propose(proposal)
        report = governance.validate(record.proposal.version_id, generate_market_data(300, start_price=40_000))
        self.assertTrue(report.passed)
        with self.assertRaises(PermissionError):
            governance.promote(record.proposal.version_id, RolloutStage.VALIDATED, actor_role="AI_AGENT")

        governance.promote(record.proposal.version_id, RolloutStage.VALIDATED)
        governance.promote(record.proposal.version_id, RolloutStage.PAPER)
        with self.assertRaises(ValueError):
            governance.promote(record.proposal.version_id, RolloutStage.TESTNET, DeploymentEvidence())
        governance.promote(
            record.proposal.version_id,
            RolloutStage.TESTNET,
            DeploymentEvidence(
                paper_days=14,
                paper_orders=20,
                max_drawdown=-0.05,
                verified=True,
                evidence_digest="b" * 64,
            ),
        )
        with self.assertRaises(ValueError):
            governance.promote(
                record.proposal.version_id,
                RolloutStage.LIVE,
                DeploymentEvidence(testnet_days=7, testnet_orders=10, requested_live_capital=100),
            )
        live = governance.promote(
            record.proposal.version_id,
            RolloutStage.LIVE,
            DeploymentEvidence(
                testnet_days=7,
                testnet_orders=10,
                max_drawdown=-0.05,
                requested_live_capital=100,
                manual_confirmation="I_UNDERSTAND_LIVE_TRADING_RISK",
                verified=True,
                evidence_digest="a" * 64,
            ),
        )
        self.assertEqual(live.stage, RolloutStage.LIVE)

    def test_strategy_version_and_audit_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategies.db"
            first = StrategyGovernanceService(
                criteria=ValidationCriteria(
                    minimum_bars=120, minimum_trades=0, minimum_sharpe=-999, maximum_drawdown=1
                ),
                storage_path=path,
            )
            record = first.propose(
                StrategyProposal("ma_cross", {"short_window": 10, "long_window": 30}, "持久化测试")
            )
            first.validate(record.proposal.version_id, generate_market_data(300))
            first.promote(record.proposal.version_id, RolloutStage.VALIDATED)
            first.close()

            recovered = StrategyGovernanceService(storage_path=path)
            loaded = recovered.get(record.proposal.version_id)
            self.assertEqual(loaded.stage, RolloutStage.VALIDATED)
            self.assertTrue(loaded.validation.passed)
            self.assertGreaterEqual(len(loaded.audit_log), 3)
            recovered.close()


class GovernedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_matching_approved_stage_can_start(self):
        governance = StrategyGovernanceService(
            criteria=ValidationCriteria(
                minimum_bars=120, minimum_trades=0, minimum_sharpe=-999, maximum_drawdown=1
            )
        )
        record = governance.propose(
            StrategyProposal("rsi_reversion", {"window": 14}, "受治理的Paper版本")
        )
        governance.validate(record.proposal.version_id, generate_market_data(300))
        governance.promote(record.proposal.version_id, RolloutStage.VALIDATED)
        governance.promote(record.proposal.version_id, RolloutStage.PAPER)
        paper = PaperExchangeAdapter("BTC/USDT", initial_cash=10_000)
        session = GovernedTradingSession(
            governance,
            record.proposal.version_id,
            RolloutStage.PAPER,
            paper,
            get_instrument("BTC/USDT"),
        )
        engine = await session.start()
        self.assertIsInstance(engine, EventDrivenTradingEngine)
        self.assertTrue(paper.connected)
        await session.stop()

        wrong_stage = GovernedTradingSession(
            governance,
            record.proposal.version_id,
            RolloutStage.TESTNET,
            paper,
            get_instrument("BTC/USDT"),
        )
        with self.assertRaises(PermissionError):
            await wrong_stage.start()

    async def test_ai_cannot_bypass_governance_for_automatic_execution(self):
        paper = PaperExchangeAdapter("BTC/USDT", initial_cash=10_000)
        with self.assertRaises(PermissionError):
            TradingRunner(
                paper,
                AIModelStrategy(),
                config=TradingRunnerConfig(dry_run=False),
            )
        with self.assertRaises(PermissionError):
            EventDrivenTradingEngine(
                paper,
                AIModelStrategy(),
                get_instrument("BTC/USDT"),
                "unapproved-ai-version",
            )


if __name__ == "__main__":
    unittest.main()
