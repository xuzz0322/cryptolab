"""Download public crypto spot bars and run the event-driven backtest."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from .backtest import BacktestEngine
from .models import BacktestConfig
from .providers import PublicCryptoProvider, CachedMarketDataProvider, MarketDataError, save_bars_csv
from .strategies import create_strategy


def _date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期格式应为 YYYY-MM-DD") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="下载虚拟币现货历史行情并回测")
    parser.add_argument("--symbol", default="BTC/USDT", choices=("BTC/USDT", "ETH/USDT", "SOL/USDT"))
    parser.add_argument("--start", required=True, type=_date)
    parser.add_argument("--end", required=True, type=_date)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/crypto-cache"))
    parser.add_argument("--backtest", choices=("ma_cross", "rsi_reversion", "bollinger", "ai_signal"), default="ma_cross")
    parser.add_argument("--cash", type=float, default=100_000.0, help="初始 USDT")
    args = parser.parse_args()
    try:
        provider = CachedMarketDataProvider(PublicCryptoProvider(), args.cache_dir)
        bars = provider.get_daily_bars(args.symbol, args.start, args.end)
        if len(bars) < 2:
            raise MarketDataError("行情不足，请检查币对、日期或网络访问")
        if args.output:
            save_bars_csv(args.output, bars)
            print("行情已保存到 %s" % args.output)
        result = BacktestEngine(BacktestConfig(initial_cash=args.cash)).run(
            bars, create_strategy(args.backtest), symbol=args.symbol
        )
        print("已加载 %d 个 UTC 交易日：%s 至 %s" % (len(bars), bars[0].date, bars[-1].date))
        print(json.dumps(result.metrics, ensure_ascii=False, indent=2))
        print("订单 %d 笔，成交回报 %d 笔" % (len(result.orders), len(result.fills)))
    except (ValueError, MarketDataError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
