"""Download real historical bars and optionally run a local backtest."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from .backtest import BacktestEngine
from .models import BacktestConfig
from .instruments import Instrument
from .market_rules import AShareRules
from decimal import Decimal
from .providers import CachedMarketDataProvider, MarketDataError, TushareProvider, save_bars_csv
from .strategies import create_strategy


def _date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期格式应为 YYYY-MM-DD") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="下载真实 A 股历史行情")
    parser.add_argument("--symbol", required=True, help="例如 000001.SZ 或 600519.SH")
    parser.add_argument("--start", required=True, type=_date)
    parser.add_argument("--end", required=True, type=_date)
    parser.add_argument("--adjust", choices=("qfq", "none"), default="qfq")
    parser.add_argument("--output", type=Path, help="另存为指定 CSV")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    parser.add_argument("--backtest", choices=("ma_cross", "rsi_reversion", "bollinger", "ai_signal"))
    args = parser.parse_args()

    try:
        provider = CachedMarketDataProvider(
            TushareProvider(adjust=args.adjust), cache_dir=args.cache_dir
        )
        bars = provider.get_daily_bars(args.symbol, args.start, args.end)
        if not bars:
            raise MarketDataError("指定区间没有行情，请检查代码、日期和接口权限")
        if args.output:
            save_bars_csv(args.output, bars)
            print("行情已保存到 %s" % args.output)
        print("已加载 %d 个交易日：%s 至 %s" % (len(bars), bars[0].date, bars[-1].date))
        if args.backtest:
            instrument = Instrument(
                args.symbol, "A_SHARE", args.symbol, "CNY", Decimal("0.01"),
                Decimal("100"), Decimal("100"), Decimal("0")
            )
            result = BacktestEngine(
                BacktestConfig(
                    lot_size=100, commission_rate=0.0003, taker_fee_rate=0.0003,
                    slippage_rate=0.0005, settlement_days=1, minimum_notional=0
                )
            ).run(
                bars, create_strategy(args.backtest), symbol=args.symbol,
                market_rules=AShareRules(), instrument=instrument
            )
            print(json.dumps(result.metrics, ensure_ascii=False, indent=2))
    except (ValueError, MarketDataError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
