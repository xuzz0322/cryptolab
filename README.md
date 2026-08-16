# CryptoLab：虚拟币现货量化研究系统

这是一个面向量化开发求职的虚拟币现货研究项目。核心研究、回测与Paper功能只使用 Python 标准库；Binance与OKX实时用户流使用可选的 `websockets` 依赖。系统实现从 7×24 行情、币对主数据、技术指标、信号生成、风险控制、OMS、模拟撮合到绩效分析的完整链路，并提供交互式网页仪表盘。

> 系统只用于学习和研究，不构成投资建议。公共行情适配器只读取无需密钥的 K 线，不具有下单或提币权限。

新的推荐主链路采用“事件驱动、策略与执行解耦、独立风控、OMS状态机、交易所权威账本对账和策略版本治理”。详细设计见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 立即运行

要求 Python 3.9 或以上版本：

```bash
python3 -m quant_system
```

如需连接 Binance Spot Testnet 或 OKX Demo Trading 的实时用户订单流，先安装实时交易依赖：

```bash
python3 -m pip install -e '.[live]'
```

打开 <http://127.0.0.1:8000>。无需安装依赖。运行测试：

```bash
python3 -m unittest discover -v
```

也可以在 Python 中使用核心引擎：

```python
from quant_system import BacktestConfig, BacktestEngine, create_strategy, generate_market_data

bars = generate_market_data(days=520, seed=42)
strategy = create_strategy("ma_cross", {"short_window": 20, "long_window": 60})
config = BacktestConfig(initial_cash=100_000, max_position_weight=0.95)
result = BacktestEngine(config).run(bars, strategy, symbol="BTC/USDT")
print(result.metrics)
```

## 已实现的量化技术

| 模块 | 技术点 | 面试中可以怎样解释 |
| --- | --- | --- |
| 币对主数据 | BTC/USDT、ETH/USDT、SOL/USDT 的价格与数量精度 | 交易数量必须按 step size 截断，同时满足最小名义金额 |
| 数据层 | Coinbase/Binance 公开日线、CSV、缓存、模拟数据 | 交易所原始数据统一为 UTC、时间升序的 OHLCV，并支持行情源回退 |
| 指标层 | SMA、EMA、滚动标准差、RSI、收益率 | 指标只使用当日及历史数据，不读取未来数据 |
| 策略层 | 双均线趋势、RSI 均值回归、布林带均值回归 | 策略只输出目标仓位，与撮合和账户解耦 |
| AI策略层 | 特征工程、ProbabilityModel、概率阈值和滞回控制 | 模型只输出概率，不能访问账户、密钥或交易接口 |
| 回测层 | 事件驱动、次日开盘成交、组合账户记账 | T 日收盘生成信号，下一交易日开盘发单，避免前视偏差 |
| OMS | 订单状态机、幂等键、部分成交、撤单、拒单、SQLite 恢复 | 订单和成交分离，一张订单可以对应多笔 Fill |
| 市场规则 | CryptoSpotRules：7×24、T+0、小数数量、最小名义金额 | 市场规则和交易核心解耦，未来可以替换为股票规则 |
| 模拟交易所 | 市价/限价撮合、成交量参与率、部分成交、T+0 | 不能成交也是回测结果的一部分，不能默认每个信号都满额成交 |
| 成本模型 | Maker/Taker 费率、方向滑点、价格与数量精度 | 理想价格不等于可成交价格，费用会侵蚀高换手策略 |
| 风控层 | 仓位、止损、单笔金额、日亏损/回撤熔断、价格偏离 | 在订单提交前统一检查，并记录明确的拒单原因 |
| 绩效层 | CAGR、波动率、Sharpe、Sortino、最大回撤、Calmar、胜率、Profit Factor | 收益之外还要衡量为收益承担了多少风险 |
| 对照实验 | Buy & Hold 基准、超额收益 | 没有基准的收益率无法说明策略是否创造了 Alpha |

## 系统结构

```text
行情 Bar
  → Strategy 计算指标并产生目标仓位
  → RiskManager 策略级与订单级风控
  → OMS 创建订单并推进状态
  → CryptoSpotRules 检查币对精度与最小金额
  → SimulatedBroker 在下一根 K 线开盘撮合
  → Fill 更新 Portfolio USDT现金 / T+0币仓 / 净值
  → OMS 持久化订单及成交
  → Metrics 计算风险收益指标
  → HTTP API → Web Dashboard
```

```text
quant_system/
├── data.py          # CSV 与模拟行情
├── providers.py     # Binance/Tushare/CSV 行情适配器
├── instruments.py   # 币对精度及最小交易金额
├── market_rules.py  # 币圈现货与 A 股规则插件
├── exchange.py      # 异步 ExchangeAdapter、用户流事件与执行路由
├── events.py        # 标准交易事件与异步事件总线
├── event_engine.py  # 解耦后的事件驱动交易主链路
├── rate_limit.py    # REST请求权重和订单频率限流器
├── alerts.py        # HTTPS Webhook告警、重试、去重和脱敏
├── evidence.py      # Paper/Testnet运行证据与SHA-256摘要
├── risk_service.py  # 独立、Fail-Closed的交易前风控
├── ledger.py        # 交易所权威账本与对账
├── strategy_governance.py # AI提案、验证和版本审计
├── deployment.py    # Paper→Testnet→Live部署门禁
├── governed_runtime.py # 受治理的唯一推荐运行入口
├── adapters/
│   ├── paper.py     # Paper Exchange 完整模拟适配器
│   ├── binance.py   # Binance Spot REST与WebSocket用户流适配器
│   └── okx.py       # OKX V5 Spot Demo/Live REST与私有用户流
├── indicators.py    # 技术指标
├── strategies.py    # 策略接口及四个示例策略
├── ai_strategy.py   # AI特征、模型接口和概率策略
├── runner.py        # 默认Dry Run的异步策略运行器
├── risk.py          # 交易前风控
├── order_models.py  # 订单、成交及状态枚举
├── oms.py           # 订单状态机及 SQLite 仓储
├── broker.py        # 模拟交易所和日线撮合
├── portfolio.py     # USDT现金、T+0币仓和盈亏账本
├── backtest.py      # 编排策略、风控、OMS、券商和账户
├── metrics.py       # 绩效分析
├── api.py           # JSON API 和静态文件服务
└── static/          # 零依赖可视化仪表盘
```

## API

- `GET /api/health`：健康检查
- `GET /api/strategies`：策略及默认参数
- `POST /api/backtest`：运行回测

示例请求：

```bash
curl -X POST http://127.0.0.1:8000/api/backtest \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"BTC/USDT","data_source":"synthetic","strategy":"ma_cross","params":{"short_window":20,"long_window":60},"days":520}'
```

CSV 输入需包含 `date, open, high, low, close, volume` 六列，日期支持 `YYYY-MM-DD`、`YYYY/MM/DD` 和 `YYYYMMDD`。

## 接入真实虚拟币行情

公共日线接口不需要 API Key。系统优先使用 Coinbase，失败后回退 Binance。下载 BTC/USDT 并直接运行回测：

```bash
python3 -m quant_system.crypto_data \
  --symbol BTC/USDT \
  --start 2021-01-01 \
  --end 2026-08-01 \
  --output data/BTC_USDT.csv \
  --backtest ma_cross
```

第一次调用会访问公开 REST API，标准化后缓存在 `data/crypto-cache/`；相同币对和日期范围的后续请求直接读取缓存。数量单位是基础资产，例如 BTC/USDT 的 `volume` 表示 BTC 数量。日期按 UTC 处理。

网页仪表盘也可以把“行情来源”切换为“公共交易所日线”。若所在网络无法访问这些交易所，仍可使用离线模拟数据或导入 CSV。

公开历史行情不需要密钥。交易执行层另行提供默认关闭下单的异步 ExchangeAdapter；建议只连接 Paper 或测试网，不要直接管理真实资金。

## Exchange Adapter

系统已经提供统一异步接口：

```text
ExchangeAdapter
├── PaperExchangeAdapter
├── BinanceSpotTestnetAdapter
└── OKXSpotAdapter

ExecutionRouter
└── 根据 venue 将订单路由到对应 Adapter
```

接口包括连接、断开、下单、撤单、订单查询、活动订单、余额、持仓、成交、品种信息、行情流、私有用户事件流和账户对账。

Paper Exchange 可以不使用网络完成完整流程：

```python
import asyncio
from datetime import date

from quant_system import Bar, Order, Side, PaperExchangeAdapter

async def demo():
    exchange = PaperExchangeAdapter("BTC/USDT", initial_cash=10_000)
    await exchange.connect()
    await exchange.publish_bar(Bar(date(2025, 1, 2), 95000, 97000, 94000, 96500, 1000))
    order = Order("BTC/USDT", Side.BUY, 0.01, date(2025, 1, 2))
    result = await exchange.submit_order(order)
    snapshot = await exchange.reconcile()
    print(result.status, snapshot.to_dict())

asyncio.run(demo())
```

Binance Adapter 默认连接 Spot Testnet，且交易开关默认关闭：

```bash
export BINANCE_TESTNET_API_KEY='测试网 Key'
export BINANCE_TESTNET_API_SECRET='测试网 Secret'
```

```python
from quant_system import BinanceSpotTestnetAdapter

# 只连接和查询，无法下单
exchange = BinanceSpotTestnetAdapter()

# 只有显式开启后才能向测试网提交订单
test_exchange = BinanceSpotTestnetAdapter(trading_enabled=True)
```

测试网完整连接步骤：

1. 在 Binance Spot Test Network 创建仅用于测试网的 API Key/Secret；不要填生产密钥。
2. 安装 `.[live]` 可选依赖并通过环境变量注入密钥。
3. 创建 `BinanceSpotTestnetAdapter(trading_enabled=True)`，不要修改默认测试网 URL。
4. 通过 `GovernedTradingSession` 启动已晋级到 `TESTNET` 的策略；引擎会并发消费行情流和用户订单流。
5. 先观察订单、成交、余额、告警和每日证据；满足门禁后再考虑下一阶段。

私有用户流启动后会自动创建 Listen Key，每 30 分钟续期，并把 `executionReport` 转为标准订单更新和实时 `FillEvent`。连接中断、90 秒无消息或 Listen Key 失效时，会关闭旧连接、指数退避并重新创建连接。REST请求统一经过请求权重/订单窗口限流器，避免各策略直接冲击交易所接口。

安全约束：

- 凭证只从环境变量读取，`.env` 已排除在 Git 之外；
- 默认 `trading_enabled=False`，提交订单会抛出 `TradingDisabledError`；
- 默认 URL 是 `https://testnet.binance.vision`；
- 生产 URL 还需要显式设置 `allow_live=True`，避免误连；
- Paper和Testnet适配器都提供Kill Switch；
- REST请求包含时间同步、`recvWindow`、HMAC SHA-256签名和客户端订单幂等编号；
- 私有WebSocket推送与REST响应采用订单/成交ID去重，OMS不会重复记账；
- Listen Key自动创建、30分钟续期、失效重建，用户流支持指数退避自动重连；
- REST统一限制每分钟请求权重、每10秒订单数和每日订单数；
- `reconcile()`会重新查询余额、持仓和活动订单，交易所状态作为最终事实。

当前 Binance Adapter 使用REST下单和查询，私有WebSocket接收订单、成交和余额事件；公开K线行情仍采用只读取已收盘K线的轮询。上述能力不等于生产就绪，在取得足够的长期运行证据、完成值班和灾备安排前，不应直接管理真实资金。

## 连接 OKX Demo Trading

OKX没有 Binance Listen Key。私有流通过签名登录WebSocket，然后订阅 `orders` 和 `account` 频道；断线后系统会重新连接、重新登录并重新订阅。

先在 OKX 的模拟交易环境创建 Demo Trading API Key，权限只选择“读取”和“交易”，不要授予提币权限。记录创建时设置的 Passphrase，然后配置：

```bash
python3 -m pip install -e '.[live]'

export OKX_API_KEY='你的Demo API Key'
export OKX_API_SECRET='你的Demo API Secret'
export OKX_API_PASSPHRASE='创建Key时设置的Passphrase'
```

默认构造器使用 Demo Trading、交易关闭，并自动发送 `x-simulated-trading: 1`：

```python
from quant_system import OKXSpotAdapter

# 只连接、查询与接收私有推送，不能下单
read_only = OKXSpotAdapter()

# 显式允许向OKX模拟盘下单
okx_demo = OKXSpotAdapter(trading_enabled=True)
await okx_demo.connect()
snapshot = await okx_demo.reconcile()
print(snapshot.to_dict())
```

实际策略运行仍必须通过 `GovernedTradingSession`，并且策略版本已人工晋级到 `RolloutStage.TESTNET`：

```python
session = GovernedTradingSession(
    governance=registry,
    version_id=approved_version,
    stage=RolloutStage.TESTNET,
    adapter=okx_demo,
    instrument=get_instrument("BTC/USDT"),
    alert_manager=alerts,
    evidence_store=evidence,
)
engine = await session.start()
await engine.run()
```

安全约束：

- `demo=True`是默认值，模拟交易请求会携带OKX要求的模拟盘请求头；
- `trading_enabled=False`是默认值，防止连接后立即下单；
- 实盘必须同时设置 `demo=False, allow_live=True, trading_enabled=True`，且策略阶段必须为 `LIVE`；
- REST签名使用 `timestamp + method + requestPath + body` 的 HMAC SHA-256/Base64；
- 私有WebSocket密钥只用于登录签名，不写入事件、日志或告警；
- 用户流包含25秒空闲检测、`ping/pong`心跳、指数退避重连和外部告警；
- `orders`频道的状态和成交会转换为统一 `OrderUpdateEvent`/`FillEvent`，继续经过OMS、账本与风控链路；
- REST调用继续使用系统统一限流器，账户与活动订单由 `reconcile()` 对账。

建议先保持 `trading_enabled=False` 验证时间同步、余额、私有WebSocket和Webhook告警，再打开Demo交易开关。不要把生产API Key与Demo API Key混用。

## 外部告警与运行证据

告警支持任意 HTTPS Webhook，包含冷却去重、失败重试、字段白名单和URL/API密钥脱敏：

```python
from pathlib import Path
from quant_system import AlertManager, RuntimeEvidenceStore, WebhookAlertSink

alerts = AlertManager(
    [WebhookAlertSink("https://alerts.example.com/quant-hook")],
    cooldown_seconds=60,
)
evidence = RuntimeEvidenceStore(Path("runtime/testnet-evidence.db"))

session = GovernedTradingSession(
    # governance/version_id/stage/adapter/instrument 参数同架构文档
    alert_manager=alerts,
    evidence_store=evidence,
    governance=registry,
    version_id=approved_version,
    stage=RolloutStage.TESTNET,
    adapter=test_exchange,
    instrument=get_instrument("BTC/USDT"),
)
engine = await session.start()
await engine.run()
```

运行期间，系统把每日心跳、订单、回撤、对账错误和运行错误持久化到SQLite。晋级证据必须由数据库生成，人工填写的天数或订单数不能通过门禁：

```python
deployment_evidence = evidence.build_evidence(RolloutStage.TESTNET)
```

每份证据都绑定其原始记录的SHA-256摘要。代码已具备长期证据采集能力，但“Paper ≥14个实际运行日”和“Testnet ≥7个实际运行日”必须由程序真实连续运行积累，不能用单元测试或手工数据代替。建议由进程管理器常驻运行，并对进程存活、磁盘空间、Webhook送达和SQLite备份另设基础设施监控。

## AI策略放在哪里

AI位于策略层，不能放在Exchange Adapter或绕过风控：

```text
行情 Bar
  → FeatureVector 特征工程
  → ProbabilityModel 模型推理
  → AIModelStrategy 生成目标仓位
  → TradingRunner 限额、限频和Dry Run
  → Adapter风控 / OMS
  → Exchange Adapter
```

当前特征包括5/20期动量、20期波动率、RSI和价格相对20期均线的偏离。`LinearProbabilityModel`是零依赖的接口演示模型，不代表已经训练出有效Alpha。可以将它替换为sklearn、XGBoost、PyTorch或ONNX模型，只需实现：

```python
from quant_system import ProbabilityModel

class MyModel(ProbabilityModel):
    def predict_probability(self, features):
        values = features.to_dict()
        # 调用经过样本外验证的本地模型
        return 0.62
```

模型接入策略：

```python
from quant_system import AIModelStrategy

strategy = AIModelStrategy(
    model=MyModel(),
    entry_probability=0.60,
    exit_probability=0.45,
)
```

异步运行器默认只生成订单计划，不提交：

```python
from quant_system import TradingRunner, TradingRunnerConfig

runner = TradingRunner(
    adapter=exchange,
    strategy=strategy,
    symbol="BTC/USDT",
    config=TradingRunnerConfig(
        dry_run=True,
        max_position_weight=0.20,
        max_order_notional=100,
        max_orders_per_day=1,
    ),
)

await runner.run()
```

只有完成回测、样本外验证、Paper运行和测试网观察后，才应人工将 `dry_run` 改为 `False`。即使关闭Dry Run，Exchange Adapter自己的交易开关和Kill Switch仍然生效。

AI训练必须与在线推理解耦。推荐离线完成标签构造、时间序列切分、训练和模型版本登记；在线服务只加载固定版本模型。禁止把未来收益、未来K线或尚未结束的K线放入特征，避免数据泄漏。

## 策略研究与过拟合门禁

`quant_system.research`提供独立于交易运行时的统计研究流水线：

- 按时间顺序固定划分50%研究、25%验证、25%最终测试，禁止随机打乱；
- 只在验证集选择预注册候选，最终测试集用于一次性审计；
- 导出选中策略逐日收益和所有候选的`T×N`收益矩阵；
- 计算PSR/DSR、CSCV PBO、Holm多重检验折损Sharpe和Minimum Track Record Length；
- 执行1倍、1.5倍、2倍成本压力测试；
- 报告90日walk-forward窗口和BTC/ETH/SOL跨币种稳健性；
- 币圈日频使用365期年化；少于10个候选时PBO不伪造结果，报告直接失败；
- 报告与候选参数SHA-256指纹绑定，失败报告不能通过策略治理晋级。

先下载三个币对的同区间数据：

```bash
python3 -m quant_system.crypto_data --symbol BTC/USDT --start 2021-01-01 --end 2026-08-15 --output data/BTC_USDT.csv
python3 -m quant_system.crypto_data --symbol ETH/USDT --start 2021-01-01 --end 2026-08-15 --output data/ETH_USDT.csv
python3 -m quant_system.crypto_data --symbol SOL/USDT --start 2021-01-01 --end 2026-08-15 --output data/SOL_USDT.csv
```

复制并冻结预注册配置。运行后不能根据最终测试集结果修改同一轮配置：

```bash
cp research_config.example.json runtime/research_config_v1.json
python3 -m quant_system.research \
  --config runtime/research_config_v1.json \
  --output-dir runtime/research-v1
```

未通过时命令退出码为2，这是统计门禁的正常行为。输出包括：

```text
runtime/research-v1/
├── research_report.json
├── selected_returns.csv
└── trials_matrix.csv
```

只有`research_report.json`中的`passed`为`true`时，才能把报告绑定到完全相同的策略指纹。治理服务应使用`ValidationCriteria(require_research_report=True)`并调用`record_research_validation()`；修改任何策略参数都会导致指纹不匹配。服务器包若命名为`cryptolab`，将上述命令中的`quant_system`替换为`cryptolab`。

## 保留的 A 股迁移接口

股票不是当前默认市场，但 `AShareRules` 和 Tushare 历史行情适配器仍然保留，作为系统成熟后迁移股票市场的插件。Token 只通过环境变量设置：

```bash
export TUSHARE_TOKEN='你的 Token'
```

下载前复权日线，同时用真实数据运行双均线回测：

```bash
python3 -m quant_system.market_data \
  --symbol 000001.SZ \
  --start 2020-01-01 \
  --end 2025-12-31 \
  --adjust qfq \
  --output data/000001_SZ.csv \
  --backtest ma_cross
```

第一次请求会访问 Tushare，标准化后缓存在 `data/cache/`；相同股票和日期范围的后续请求直接读取缓存。Tushare 的 `vol` 单位为手，适配器会转换成股。前复权模式使用区间最后一个交易日的复权因子作为基准，使最新价格保持不变。

也可以在代码中使用：

```python
from datetime import date
from quant_system import CachedMarketDataProvider, TushareProvider

provider = CachedMarketDataProvider(TushareProvider(adjust="qfq"))
bars = provider.get_daily_bars("600519.SH", date(2020, 1, 1), date(2025, 12, 31))
```

接口实现位于 `quant_system/providers.py`。若以后更换供应商，只需实现：

```python
class AnotherProvider(MarketDataProvider):
    def get_daily_bars(self, symbol, start, end):
        # 拉取供应商数据，并转换为按日期升序的 List[Bar]
        ...
```

股票回测命令会显式启用 `AShareRules`，恢复 T+1、100股整手和涨跌停规则。核心策略、OMS、绩效和行情接口不需要改写。

## OMS 与模拟交易链路

订单状态遵循受控状态机：

```text
CREATED → RISK_CHECKED → SUBMITTED → ACCEPTED
                                      ├→ PARTIALLY_FILLED → FILLED
                                      ├→ CANCELLED
                                      └→ REJECTED
```

系统提供两种订单仓储：回测默认使用 `InMemoryOrderRepository`；需要在程序重启后恢复订单时使用 SQLite：

```python
from pathlib import Path
from quant_system import OrderManager, SqliteOrderRepository

repository = SqliteOrderRepository(Path("runtime/oms.db"))
oms = OrderManager(repository)
```

每个订单具有 `order_id` 和调用方提供的 `client_order_id`。相同 `client_order_id` 重复提交时返回原订单，用于防止网络重试造成重复下单。订单与成交分别保存，一张大订单受成交量参与率限制时会进入 `PARTIALLY_FILLED`，剩余部分可以继续撮合或撤销。

模拟交易所当前支持：

- 市价单和限价单触价判断；
- 买卖方向滑点和 Maker/Taker 费率；
- 按当日成交量比例限制最大成交数量；
- T+0：现货买入后立即成为可用持仓；
- 币对价格精度、数量步长、最小数量和最小名义金额；
- 资金不足、限价偏离、单币仓位、日亏损和回撤熔断。

回测返回值和 `/api/backtest` 响应同时包含 `orders`、`fills` 与兼容旧界面的 `trades`。仪表盘可以查看每张订单的委托数量、成交数量、均价、最终状态和拒绝原因。

## 金融基础速查

- **收益率**：资产价值的相对变化。跨不同本金比较时用收益率，不用绝对盈亏。
- **波动率**：收益率标准差的年化值，表示价格不确定程度，不完全等同于亏损风险。
- **最大回撤**：净值从历史高点到随后低点的最大跌幅，直观描述最痛苦的持有阶段。
- **Sharpe Ratio**：单位总风险获得的超额收益，越高通常越好，但它依赖收益近似平稳等假设。
- **Alpha / Beta**：Alpha 是无法被基准风险暴露解释的收益；Beta 描述组合对市场变化的敏感度。本项目先用策略相对 Buy & Hold 的超额收益作入门对照，它并不是严格的回归 Alpha。
- **趋势与均值回归**：趋势策略假设上涨倾向延续；均值回归策略假设极端偏离会被修复，两者适用的市场状态不同。
- **过拟合**：在历史样本上反复调参会把噪声当规律。应保留样本外数据，并使用滚动或 walk-forward 验证。

## 当前边界与可继续扩展

目前是具有完整模拟订单链路以及 Binance/OKX 私有用户流的单币对、日频、现货 long-only 研究系统，但仍不是生产实盘平台。推荐依次扩展：

1. 将公开日线轮询扩展为 WebSocket 逐笔/深度行情，并建立本地K线聚合。
2. 支持多币种组合、再平衡和风险平价仓位。
3. 加入样本内/样本外拆分、网格搜索和 walk-forward 分析。
4. 用独立进程监控、不可变审计存储和灾备演练强化运维。
5. 连续积累并审核真实的14日Paper与7日Testnet证据。

永续合约尚未实现。它需要独立的保证金、杠杆、标记价格、资金费率、Reduce-only 和强平模型，不能直接复用现货账户假装完成。

面试时不要只展示收益最高的参数。更有价值的是解释数据边界、如何避免未来函数、成本假设、失败市场状态以及下一步如何验证稳健性。
