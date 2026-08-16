"""Market-data provider abstractions and a Tushare implementation."""

import csv
import json
import os
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from .data import load_csv
from .models import Bar


class MarketDataError(RuntimeError):
    """A safe, user-facing market-data error."""


class MarketDataProvider(ABC):
    @abstractmethod
    def get_daily_bars(self, symbol: str, start: date, end: date) -> List[Bar]:
        """Return normalized daily bars in ascending date order."""


class CsvMarketDataProvider(MarketDataProvider):
    def __init__(self, path: Path):
        self.path = Path(path)

    def get_daily_bars(self, symbol: str, start: date, end: date) -> List[Bar]:
        del symbol
        return [bar for bar in load_csv(self.path) if start <= bar.date <= end]


Transport = Callable[[str, Dict[str, Any], float], Dict[str, Any]]
GetTransport = Callable[[str, Dict[str, Any], float], Any]


def _post_json(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise MarketDataError("无法连接行情服务：%s" % exc) from exc


def _get_json(url: str, params: Dict[str, Any], timeout: float) -> Any:
    request = Request(url + "?" + urlencode(params), headers={"User-Agent": "QuantLab/0.2"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise MarketDataError("无法连接数字资产行情服务：%s" % exc) from exc


class BinanceSpotProvider(MarketDataProvider):
    """Public Binance spot daily K-lines; no API key or trading permission required."""

    endpoint = "https://api.binance.com/api/v3/klines"

    def __init__(self, timeout: float = 15.0, transport: GetTransport = _get_json):
        self.timeout = timeout
        self.transport = transport

    @staticmethod
    def exchange_symbol(symbol: str) -> str:
        return symbol.upper().replace("/", "").replace("-", "").replace("_", "")

    def get_daily_bars(self, symbol: str, start: date, end: date) -> List[Bar]:
        if start > end:
            raise ValueError("start 不能晚于 end")
        start_ms = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
        end_ms = int(datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc).timestamp() * 1000)
        cursor = start_ms
        bars: List[Bar] = []
        while cursor <= end_ms:
            payload = self.transport(
                self.endpoint,
                {
                    "symbol": self.exchange_symbol(symbol),
                    "interval": "1d",
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
                self.timeout,
            )
            if isinstance(payload, dict):
                raise MarketDataError("Binance 返回错误：%s" % payload.get("msg", payload))
            if not payload:
                break
            for item in payload:
                trading_date = datetime.utcfromtimestamp(int(item[0]) / 1000).date()
                if start <= trading_date <= end:
                    bars.append(
                        Bar(
                            date=trading_date,
                            open=float(item[1]),
                            high=float(item[2]),
                            low=float(item[3]),
                            close=float(item[4]),
                            volume=float(item[5]),
                        )
                    )
            next_cursor = int(payload[-1][0]) + 86_400_000
            if next_cursor <= cursor or len(payload) < 1000:
                break
            cursor = next_cursor
        unique = {bar.date: bar for bar in bars}
        return [unique[key] for key in sorted(unique)]


class CoinbaseSpotProvider(MarketDataProvider):
    """Public Coinbase Exchange candles, requested in <=300-day windows."""

    endpoint = "https://api.exchange.coinbase.com/products/{product}/candles"

    def __init__(self, timeout: float = 15.0, transport: GetTransport = _get_json):
        self.timeout = timeout
        self.transport = transport

    def get_daily_bars(self, symbol: str, start: date, end: date) -> List[Bar]:
        if start > end:
            raise ValueError("start 不能晚于 end")
        from datetime import timedelta

        product = symbol.upper().replace("/", "-").replace("_", "-")
        cursor = start
        bars: List[Bar] = []
        while cursor <= end:
            window_end = min(end, cursor + timedelta(days=299))
            payload = self.transport(
                self.endpoint.format(product=product),
                {
                    "granularity": 86400,
                    "start": cursor.isoformat() + "T00:00:00Z",
                    "end": window_end.isoformat() + "T23:59:59Z",
                },
                self.timeout,
            )
            if isinstance(payload, dict):
                raise MarketDataError("Coinbase 返回错误：%s" % payload.get("message", payload))
            for item in payload or []:
                trading_date = datetime.fromtimestamp(int(item[0]), tz=timezone.utc).date()
                if start <= trading_date <= end:
                    bars.append(
                        Bar(
                            date=trading_date,
                            open=float(item[3]),
                            high=float(item[2]),
                            low=float(item[1]),
                            close=float(item[4]),
                            volume=float(item[5]),
                        )
                    )
            cursor = window_end + timedelta(days=1)
        unique = {bar.date: bar for bar in bars}
        return [unique[key] for key in sorted(unique)]


class PublicCryptoProvider(MarketDataProvider):
    """Try public exchanges in order so research is not tied to one venue."""

    def __init__(self, providers: Optional[Sequence[MarketDataProvider]] = None):
        self.providers = list(providers or [CoinbaseSpotProvider(), BinanceSpotProvider()])

    def get_daily_bars(self, symbol: str, start: date, end: date) -> List[Bar]:
        errors = []
        for provider in self.providers:
            try:
                bars = provider.get_daily_bars(symbol, start, end)
                if bars:
                    return bars
                errors.append("%s: 无数据" % type(provider).__name__)
            except MarketDataError as exc:
                errors.append("%s: %s" % (type(provider).__name__, exc))
        raise MarketDataError("所有公共行情源均不可用；" + "；".join(errors))


class TushareProvider(MarketDataProvider):
    """Read A-share daily bars from the official Tushare HTTP API.

    Prices are returned as forward-adjusted bars by default. The latest close in
    the requested range remains unchanged, while older OHLC values are scaled
    using the adjustment factor. Volume is normalized from lots to shares.
    """

    endpoint = "https://api.tushare.pro"

    def __init__(
        self,
        token: Optional[str] = None,
        adjust: str = "qfq",
        timeout: float = 15.0,
        transport: Transport = _post_json,
    ):
        self.token = token or os.environ.get("TUSHARE_TOKEN", "")
        if not self.token:
            raise ValueError("缺少 Tushare Token，请设置环境变量 TUSHARE_TOKEN")
        if adjust not in ("qfq", "none"):
            raise ValueError("adjust 只支持 qfq 或 none")
        self.adjust = adjust
        self.timeout = timeout
        self.transport = transport

    def _query(self, api_name: str, symbol: str, start: date, end: date, fields: str) -> List[Dict[str, Any]]:
        payload = {
            "api_name": api_name,
            "token": self.token,
            "params": {
                "ts_code": symbol,
                "start_date": start.strftime("%Y%m%d"),
                "end_date": end.strftime("%Y%m%d"),
            },
            "fields": fields,
        }
        response = self.transport(self.endpoint, payload, self.timeout)
        if response.get("code") != 0:
            raise MarketDataError("Tushare 返回错误：%s" % (response.get("msg") or response.get("code")))
        data = response.get("data") or {}
        fields_result, items = data.get("fields") or [], data.get("items") or []
        if not fields_result:
            return []
        return [dict(zip(fields_result, item)) for item in items]

    def get_daily_bars(self, symbol: str, start: date, end: date) -> List[Bar]:
        if start > end:
            raise ValueError("start 不能晚于 end")
        if not symbol or "." not in symbol:
            raise ValueError("A 股代码应使用 Tushare 格式，例如 000001.SZ 或 600519.SH")
        rows = self._query(
            "daily", symbol.upper(), start, end, "ts_code,trade_date,open,high,low,close,vol"
        )
        if not rows:
            return []
        factors: Dict[str, float] = {}
        if self.adjust == "qfq":
            factor_rows = self._query("adj_factor", symbol.upper(), start, end, "trade_date,adj_factor")
            factors = {str(row["trade_date"]): float(row["adj_factor"]) for row in factor_rows}
            if not factors:
                raise MarketDataError("未取得复权因子，无法生成前复权行情")
            latest_factor = factors[max(factors)]
        else:
            latest_factor = 1.0

        bars: List[Bar] = []
        for row in rows:
            trade_date = str(row["trade_date"])
            factor = factors.get(trade_date, latest_factor) / latest_factor
            bars.append(
                Bar(
                    date=datetime.strptime(trade_date, "%Y%m%d").date(),
                    open=float(row["open"]) * factor,
                    high=float(row["high"]) * factor,
                    low=float(row["low"]) * factor,
                    close=float(row["close"]) * factor,
                    volume=int(float(row["vol"]) * 100),
                )
            )
        bars.sort(key=lambda bar: bar.date)
        return bars


class CachedMarketDataProvider(MarketDataProvider):
    """Cache complete provider responses as normalized OHLCV CSV files."""

    def __init__(self, upstream: MarketDataProvider, cache_dir: Path = Path("data/cache")):
        self.upstream = upstream
        self.cache_dir = Path(cache_dir)

    def _path(self, symbol: str, start: date, end: date) -> Path:
        safe_symbol = "".join(char for char in symbol.upper() if char.isalnum() or char in "-_")
        return self.cache_dir / ("%s_%s_%s.csv" % (safe_symbol, start.isoformat(), end.isoformat()))

    def get_daily_bars(self, symbol: str, start: date, end: date) -> List[Bar]:
        path = self._path(symbol, start, end)
        if path.exists():
            return load_csv(path)
        bars = self.upstream.get_daily_bars(symbol, start, end)
        if bars:
            save_bars_csv(path, bars)
        return bars


def save_bars_csv(path: Path, bars: Sequence[Bar]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(bar.to_dict() for bar in bars)
    temporary.replace(path)
