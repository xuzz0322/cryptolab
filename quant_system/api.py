import json
import mimetypes
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import urlparse

from .backtest import BacktestEngine
from .data import generate_market_data
from .models import BacktestConfig
from .strategies import STRATEGIES, create_strategy
from .providers import PublicCryptoProvider, CachedMarketDataProvider, MarketDataError


STATIC_DIR = Path(__file__).parent / "static"


def _number(payload: Dict[str, Any], key: str, default: float) -> float:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % key)
    return float(value)


def run_backtest(payload: Dict[str, Any]) -> Dict[str, Any]:
    data_source = str(payload.get("data_source", "synthetic"))
    days = int(_number(payload, "days", 520))
    if data_source == "synthetic" and not 100 <= days <= 2500:
        raise ValueError("days must be between 100 and 2500")
    if data_source == "binance":
        data_source = "public"
    if data_source not in ("synthetic", "public"):
        raise ValueError("data_source 只支持 synthetic 或 public")
    seed = int(_number(payload, "seed", 42))
    strategy_name = str(payload.get("strategy", "ma_cross"))
    params = payload.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    config = BacktestConfig(
        initial_cash=_number(payload, "initial_cash", 100_000),
        commission_rate=_number(payload, "commission_rate", 0.0003),
        slippage_rate=_number(payload, "slippage_rate", 0.0002),
        max_position_weight=_number(payload, "max_position_weight", 0.95),
        stop_loss_pct=_number(payload, "stop_loss_pct", 0.08),
        lot_size=_number(payload, "lot_size", 0.00001),
        max_order_value=(
            _number(payload, "max_order_value", 0) if payload.get("max_order_value") is not None else None
        ),
        max_daily_loss_pct=_number(payload, "max_daily_loss_pct", 0.05),
        max_drawdown_halt_pct=_number(payload, "max_drawdown_halt_pct", 0.20),
        max_volume_participation=_number(payload, "max_volume_participation", 0.10),
        price_limit_pct=(
            _number(payload, "price_limit_pct", 0) if payload.get("price_limit_pct") is not None else None
        ),
        minimum_commission=_number(payload, "minimum_commission", 0.0),
        sell_tax_rate=_number(payload, "sell_tax_rate", 0.0),
        maker_fee_rate=_number(payload, "maker_fee_rate", 0.0008),
        taker_fee_rate=_number(payload, "taker_fee_rate", payload.get("commission_rate", 0.001)),
        settlement_days=int(_number(payload, "settlement_days", 0)),
        minimum_notional=_number(payload, "minimum_notional", 5.0),
    )
    strategy = create_strategy(strategy_name, params)
    symbol = str(payload.get("symbol", "BTC/USDT"))
    if data_source == "public":
        try:
            start = date.fromisoformat(str(payload.get("start_date", "2023-01-01")))
            end = date.fromisoformat(str(payload.get("end_date", date.today().isoformat())))
        except ValueError as exc:
            raise ValueError("真实行情日期格式应为 YYYY-MM-DD") from exc
        bars = CachedMarketDataProvider(
            PublicCryptoProvider(), cache_dir=Path("data/crypto-cache")
        ).get_daily_bars(symbol, start, end)
        if len(bars) < 100:
            raise ValueError("真实行情少于100根K线，请扩大日期范围或检查网络")
    else:
        bars = generate_market_data(days=days, seed=seed, start_price=40_000.0)
    result = BacktestEngine(config).run(bars, strategy, symbol=symbol).to_dict()
    result["symbol"] = symbol
    result["data_source"] = data_source
    result["bars"] = [bar.to_dict() for bar in bars]
    return result


class QuantRequestHandler(BaseHTTPRequestHandler):
    server_version = "QuantStarter/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _json(self, status: int, body: Dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _static(self, relative: str) -> None:
        path = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in path.parents and path != STATIC_DIR.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = path.read_bytes()
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
        elif path == "/api/strategies":
            self._json(
                HTTPStatus.OK,
                {
                    "strategies": [
                        {"id": "ma_cross", "name": "双均线趋势", "params": {"short_window": 20, "long_window": 60}},
                        {"id": "rsi_reversion", "name": "RSI 均值回归", "params": {"window": 14, "buy_below": 35, "sell_above": 65}},
                        {"id": "bollinger", "name": "布林带均值回归", "params": {"window": 20, "std_multiplier": 2}},
                        {"id": "ai_signal", "name": "AI 特征评分", "params": {"entry_probability": 0.55, "exit_probability": 0.45}},
                    ]
                },
            )
        elif path in ("/", "/index.html"):
            self._static("index.html")
        elif path.startswith("/static/"):
            self._static(path[len("/static/") :])
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/backtest":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 100_000:
                raise ValueError("request body must be between 1 and 100000 bytes")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
            self._json(HTTPStatus.OK, run_backtest(payload))
        except (ValueError, TypeError, json.JSONDecodeError, MarketDataError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error: %s" % exc})


def serve(host: str = "127.0.0.1", port: int = 8000) -> Tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer((host, port), QuantRequestHandler)
    return server, "http://%s:%d" % (host, server.server_address[1])
