"""OKX V5 spot adapter with Demo Trading as the fail-safe default."""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import random
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..alerts import Alert, AlertManager, AlertSeverity
from ..exchange import (
    AdapterNotConnectedError,
    AuthenticationError,
    Balance,
    ExchangeAdapter,
    ExchangeError,
    ExchangePosition,
    ExchangeUserEvent,
    TradingDisabledError,
    UserEventKind,
)
from ..instruments import INSTRUMENTS, Instrument, get_instrument
from ..models import Bar
from ..order_models import Fill, Order, OrderStatus, OrderType, Side
from ..rate_limit import AsyncExchangeRateLimiter


HttpTransport = Callable[[str, str, Dict[str, str], Optional[bytes], float], Any]
WebSocketConnector = Callable[[str], Any]


def _http_json(method: str, url: str, headers: Dict[str, str], data: Optional[bytes], timeout: float) -> Any:
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("msg", "HTTP %s" % exc.code)
        except Exception:
            message = "HTTP %s" % exc.code
        raise ExchangeError("OKX API error: %s" % message) from exc
    except Exception as exc:
        raise ExchangeError("OKX connection failed: %s" % exc) from exc
    if isinstance(payload, dict) and str(payload.get("code", "0")) != "0":
        raise ExchangeError("OKX API error %s: %s" % (payload.get("code"), payload.get("msg", "unknown")))
    return payload


async def _websocket_connect(url: str):
    try:
        import websockets
    except ImportError as exc:
        raise ExchangeError("实时用户流需要安装可选依赖：pip install '.[live]'") from exc
    return await websockets.connect(url, ping_interval=None, close_timeout=10)


class OKXSpotAdapter(ExchangeAdapter):
    """OKX spot REST/private-WebSocket adapter; demo mode is enabled by default."""

    venue = "OKX"
    base_url_default = "https://www.okx.com"
    demo_private_ws_url = "wss://wspap.okx.com:8443/ws/v5/private"
    live_private_ws_url = "wss://ws.okx.com:8443/ws/v5/private"

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        demo: bool = True,
        trading_enabled: bool = False,
        allow_live: bool = False,
        base_url: str = base_url_default,
        timeout: float = 15.0,
        transport: HttpTransport = _http_json,
        websocket_connector: WebSocketConnector = _websocket_connect,
        rate_limiter: Optional[AsyncExchangeRateLimiter] = None,
        alert_manager: Optional[AlertManager] = None,
        reconnect_max_seconds: float = 30.0,
    ):
        if not demo and not allow_live:
            raise ValueError("OKX实盘必须显式设置 demo=False 且 allow_live=True")
        self.api_key = api_key or os.environ.get("OKX_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("OKX_API_SECRET", "")
        self.passphrase = passphrase or os.environ.get("OKX_API_PASSPHRASE", "")
        self.demo = demo
        self.allow_live = allow_live
        self.trading_enabled = trading_enabled
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport
        self.websocket_connector = websocket_connector
        self.rate_limiter = rate_limiter or AsyncExchangeRateLimiter()
        self.alert_manager = alert_manager or AlertManager()
        self.reconnect_max_seconds = reconnect_max_seconds
        self._connected = False
        self._kill_switch = False
        self._time_offset_ms = 0
        self._orders: Dict[str, Order] = {}
        self._user_socket = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def private_ws_url(self) -> str:
        return self.demo_private_ws_url if self.demo else self.live_private_ws_url

    def _require_connected(self) -> None:
        if not self._connected:
            raise AdapterNotConnectedError("OKX adapter is not connected")

    def _require_auth(self) -> None:
        if not self.api_key or not self.api_secret or not self.passphrase:
            raise AuthenticationError("缺少 OKX_API_KEY/API_SECRET/API_PASSPHRASE")

    @staticmethod
    def _inst_id(symbol: str) -> str:
        return symbol.upper().replace("/", "-").replace("_", "-")

    @staticmethod
    def _symbol(inst_id: str) -> str:
        parts = inst_id.upper().split("-")
        return "/".join(parts[:2]) if len(parts) >= 2 else inst_id

    @staticmethod
    def _number(value: float) -> str:
        return ("%.16f" % value).rstrip("0").rstrip(".")

    @staticmethod
    def _iso_timestamp(epoch_ms: int) -> str:
        value = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _signature(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            (timestamp + method.upper() + request_path + body).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    async def _call(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        is_order: bool = False,
    ) -> Any:
        await self.rate_limiter.acquire(1, is_order=is_order)
        values = {key: value for key, value in (params or {}).items() if value is not None}
        query = urlencode(values) if method.upper() == "GET" else ""
        request_path = path + (("?" + query) if query else "")
        body_text = "" if method.upper() == "GET" else json.dumps(values, separators=(",", ":"))
        headers = {"Accept": "application/json", "User-Agent": "CryptoLab/0.2"}
        if self.demo:
            headers["x-simulated-trading"] = "1"
        if signed:
            self._require_auth()
            timestamp = self._iso_timestamp(int(time.time() * 1000) + self._time_offset_ms)
            headers.update({
                "OK-ACCESS-KEY": self.api_key,
                "OK-ACCESS-SIGN": self._signature(timestamp, method, request_path, body_text),
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self.passphrase,
            })
        data = body_text.encode("utf-8") if body_text else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.transport(method.upper(), self.base_url + request_path, headers, data, self.timeout),
        )

    async def connect(self) -> None:
        payload = await self._call("GET", "/api/v5/public/time")
        rows = payload.get("data", [])
        if not rows:
            raise ExchangeError("OKX time endpoint returned no data")
        self._time_offset_ms = int(rows[0]["ts"]) - int(time.time() * 1000)
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        if self._user_socket is not None:
            with contextlib.suppress(Exception):
                await self._user_socket.close()
            self._user_socket = None

    async def set_kill_switch(self, enabled: bool, cancel_open_orders: bool = True) -> None:
        self._kill_switch = enabled
        if enabled and cancel_open_orders and self._connected and self.api_key:
            for order in list(await self.get_open_orders()):
                with contextlib.suppress(ExchangeError):
                    await self.cancel_order(order.order_id, order.symbol)

    @staticmethod
    def _status(value: str) -> OrderStatus:
        return {
            "live": OrderStatus.ACCEPTED,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "filled": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELLED,
            "mmp_canceled": OrderStatus.CANCELLED,
            "order_failed": OrderStatus.REJECTED,
        }.get(value, OrderStatus.SUBMITTED)

    def _local_order(self, order_id: str) -> Optional[Order]:
        if order_id in self._orders:
            return self._orders[order_id]
        return next(
            (item for item in self._orders.values() if item.exchange_order_id == str(order_id) or item.client_order_id == str(order_id)),
            None,
        )

    def _order_from_row(self, row: Dict[str, Any], symbol: Optional[str] = None) -> Order:
        target_symbol = symbol or self._symbol(str(row.get("instId", "")))
        client_id = str(row.get("clOrdId", ""))
        order = next((item for item in self._orders.values() if item.client_order_id == client_id), None)
        event_ms = int(row.get("cTime") or row.get("uTime") or int(time.time() * 1000))
        if order is None:
            order = Order(
                target_symbol,
                Side(str(row.get("side", "buy")).upper()),
                float(row.get("sz", 0) or 0),
                datetime.fromtimestamp(event_ms / 1000, tz=timezone.utc).date(),
                OrderType.LIMIT if str(row.get("ordType", "market")) == "limit" else OrderType.MARKET,
                float(row["px"]) if float(row.get("px", 0) or 0) else None,
                client_order_id=client_id,
            )
        order.exchange_order_id = str(row.get("ordId", order.exchange_order_id or "")) or None
        order.status = self._status(str(row.get("state", "")))
        order.filled_quantity = float(row.get("accFillSz", order.filled_quantity) or 0)
        order.average_fill_price = float(row.get("avgPx", order.average_fill_price) or 0)
        order.commission = abs(float(row.get("fee", order.commission) or 0))
        if order.status == OrderStatus.REJECTED:
            order.reject_reason = str(row.get("sMsg") or row.get("msg") or "OKX拒单")
        self._orders[order.order_id] = order
        return order

    @staticmethod
    def _first_data(payload: Dict[str, Any]) -> Dict[str, Any]:
        rows = payload.get("data", [])
        if not rows:
            raise ExchangeError("OKX response contains no data")
        row = rows[0]
        if str(row.get("sCode", "0")) != "0":
            raise ExchangeError("OKX order error %s: %s" % (row.get("sCode"), row.get("sMsg", "unknown")))
        return row

    async def submit_order(self, order: Order) -> Order:
        self._require_connected()
        if not self.trading_enabled:
            raise TradingDisabledError("交易开关未开启；请仅在OKX模拟盘显式设置 trading_enabled=True")
        if self._kill_switch:
            raise TradingDisabledError("Kill Switch 已开启，禁止下单")
        instrument = get_instrument(order.symbol)
        quantity = instrument.normalize_quantity(order.quantity)
        if quantity < float(instrument.minimum_quantity):
            raise ValueError("订单数量低于交易所最小值")
        params: Dict[str, Any] = {
            "instId": self._inst_id(order.symbol),
            "tdMode": "cash",
            "clOrdId": order.client_order_id,
            "side": order.side.value.lower(),
            "ordType": order.order_type.value.lower(),
            "sz": self._number(quantity),
        }
        if order.order_type == OrderType.MARKET:
            params["tgtCcy"] = "base_ccy"
        else:
            if order.limit_price is None:
                raise ValueError("限价单缺少 limit_price")
            params["px"] = self._number(instrument.normalize_price(order.limit_price))
        order.status = OrderStatus.SUBMITTED
        self._orders[order.order_id] = order
        row = self._first_data(await self._call("POST", "/api/v5/trade/order", params, signed=True, is_order=True))
        order.exchange_order_id = str(row.get("ordId", "")) or None
        return order

    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Order:
        self._require_connected()
        order = self._local_order(order_id)
        target_symbol = symbol or (order.symbol if order else None)
        if not target_symbol:
            raise ValueError("cancel_order requires symbol for an unknown local order")
        params = {"instId": self._inst_id(target_symbol)}
        params["clOrdId" if order else "ordId"] = order.client_order_id if order else order_id
        row = self._first_data(await self._call("POST", "/api/v5/trade/cancel-order", params, signed=True, is_order=True))
        if order is None:
            order = Order(target_symbol, Side.BUY, 0, datetime.now(timezone.utc).date(), client_order_id=str(row.get("clOrdId", "")))
            order.exchange_order_id = str(row.get("ordId", "")) or None
            self._orders[order.order_id] = order
        order.status = OrderStatus.CANCELLED
        return order

    async def get_order(self, order_id: str, symbol: Optional[str] = None) -> Optional[Order]:
        self._require_connected()
        order = self._local_order(order_id)
        target_symbol = symbol or (order.symbol if order else None)
        if not target_symbol:
            return None
        params = {"instId": self._inst_id(target_symbol)}
        params["clOrdId" if order else "ordId"] = order.client_order_id if order else order_id
        row = self._first_data(await self._call("GET", "/api/v5/trade/order", params, signed=True))
        return self._order_from_row(row, target_symbol)

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Order]:
        self._require_connected()
        params = {"instType": "SPOT", "instId": self._inst_id(symbol) if symbol else None}
        payload = await self._call("GET", "/api/v5/trade/orders-pending", params, signed=True)
        return [self._order_from_row(row, symbol) for row in payload.get("data", [])]

    async def get_balances(self) -> List[Balance]:
        self._require_connected()
        payload = await self._call("GET", "/api/v5/account/balance", signed=True)
        result = []
        for account in payload.get("data", []):
            for item in account.get("details", []):
                free = float(item.get("availBal", 0) or 0)
                locked = float(item.get("frozenBal", 0) or 0)
                total = float(item.get("cashBal", free + locked) or 0)
                if total or free or locked:
                    result.append(Balance(str(item["ccy"]), free, locked))
        return result

    async def get_positions(self) -> List[ExchangePosition]:
        balances = {item.currency: item for item in await self.get_balances()}
        result = []
        for instrument in INSTRUMENTS.values():
            balance = balances.get(instrument.base_currency)
            if balance and balance.total > 0:
                result.append(ExchangePosition(instrument.symbol, balance.total, balance.free))
        return result

    async def get_fills(self, symbol: Optional[str] = None) -> List[Fill]:
        self._require_connected()
        params = {"instType": "SPOT", "instId": self._inst_id(symbol) if symbol else None}
        payload = await self._call("GET", "/api/v5/trade/fills", params, signed=True)
        result = []
        for row in payload.get("data", []):
            result.append(Fill(
                order_id=str(row.get("ordId", "")),
                symbol=symbol or self._symbol(str(row["instId"])),
                side=Side(str(row["side"]).upper()),
                quantity=float(row.get("fillSz", 0) or 0),
                price=float(row.get("fillPx", 0) or 0),
                commission=abs(float(row.get("fee", 0) or 0)),
                trading_date=datetime.fromtimestamp(int(row["ts"]) / 1000, tz=timezone.utc).date(),
                fill_id=str(row.get("tradeId", "")),
            ))
        return result

    async def get_instruments(self) -> Sequence[Instrument]:
        return [replace(item, venue=self.venue) for item in INSTRUMENTS.values()]

    def _ws_login(self) -> Dict[str, Any]:
        self._require_auth()
        timestamp = str((int(time.time() * 1000) + self._time_offset_ms) // 1000)
        signature = self._signature(timestamp, "GET", "/users/self/verify")
        return {"op": "login", "args": [{
            "apiKey": self.api_key,
            "passphrase": self.passphrase,
            "timestamp": timestamp,
            "sign": signature,
        }]}

    @staticmethod
    async def _recv_json(socket, timeout: float = 15.0) -> Dict[str, Any]:
        raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
        if raw == "pong":
            return {"event": "pong"}
        return json.loads(raw)

    async def _login_and_subscribe(self, socket) -> None:
        await socket.send(json.dumps(self._ws_login(), separators=(",", ":")))
        reply = await self._recv_json(socket)
        if reply.get("event") != "login" or str(reply.get("code", "0")) != "0":
            raise AuthenticationError("OKX WebSocket登录失败: %s" % reply.get("msg", "unknown"))
        request = {"op": "subscribe", "args": [
            {"channel": "orders", "instType": "SPOT"},
            {"channel": "account"},
        ]}
        await socket.send(json.dumps(request, separators=(",", ":")))
        subscribed = set()
        while len(subscribed) < 2:
            reply = await self._recv_json(socket)
            if reply.get("event") == "error" or str(reply.get("code", "0")) != "0":
                raise ExchangeError("OKX WebSocket订阅失败: %s" % reply.get("msg", "unknown"))
            if reply.get("event") == "subscribe":
                subscribed.add(reply.get("arg", {}).get("channel"))

    def _translate_ws_payload(self, payload: Dict[str, Any]) -> List[ExchangeUserEvent]:
        channel = payload.get("arg", {}).get("channel")
        events: List[ExchangeUserEvent] = []
        if channel == "account":
            for account in payload.get("data", []):
                balances = [
                    Balance(str(item["ccy"]), float(item.get("availBal", 0) or 0), float(item.get("frozenBal", 0) or 0))
                    for item in account.get("details", [])
                ]
                events.append(ExchangeUserEvent(UserEventKind.BALANCE, self.venue, balances=balances, raw_event_type="account"))
            return events
        if channel != "orders":
            return events
        for row in payload.get("data", []):
            order = self._order_from_row(row)
            fill_quantity = float(row.get("fillSz", 0) or 0)
            fill = None
            if fill_quantity > 0:
                fill = Fill(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=fill_quantity,
                    price=float(row.get("fillPx", 0) or 0),
                    commission=abs(float(row.get("fee", 0) or 0)),
                    trading_date=datetime.fromtimestamp(int(row.get("uTime", int(time.time() * 1000))) / 1000, tz=timezone.utc).date(),
                    fill_id=str(row.get("tradeId") or "%s-%s" % (order.order_id, row.get("uTime", 0))),
                )
            events.append(ExchangeUserEvent(
                UserEventKind.TRADE if fill else UserEventKind.ORDER,
                self.venue,
                order=order,
                fill=fill,
                raw_event_type="orders",
            ))
        return events

    async def _alert_stream_failure(self, error: Exception, attempt: int) -> None:
        await self.alert_manager.emit(
            Alert(
                "OKX用户流断开",
                "用户流将在退避后重新登录和订阅",
                AlertSeverity.WARNING if attempt < 5 else AlertSeverity.CRITICAL,
                "OKXSpotAdapter",
                {"venue": self.venue, "error": str(error), "count": attempt},
            ),
            dedupe_key="okx-user-stream-disconnect-%d" % min(attempt, 5),
        )

    async def stream_user_events(self) -> AsyncIterator[ExchangeUserEvent]:
        self._require_connected()
        attempt = 0
        while self._connected:
            socket = None
            try:
                socket = await self.websocket_connector(self.private_ws_url)
                self._user_socket = socket
                await self._login_and_subscribe(socket)
                while self._connected:
                    try:
                        payload = await self._recv_json(socket, timeout=25)
                    except asyncio.TimeoutError:
                        await socket.send("ping")
                        payload = await self._recv_json(socket, timeout=10)
                        if payload.get("event") != "pong":
                            raise ExchangeError("OKX用户流心跳超时")
                        continue
                    attempt = 0
                    for event in self._translate_ws_payload(payload):
                        yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                await self._alert_stream_failure(exc, attempt)
                delay = min(self.reconnect_max_seconds, 2 ** min(attempt - 1, 5))
                await asyncio.sleep(delay + random.random() * min(1.0, delay * 0.1))
            finally:
                if socket is not None:
                    with contextlib.suppress(Exception):
                        await socket.close()
                self._user_socket = None

    async def stream_bars(self) -> AsyncIterator[Bar]:
        """Poll the latest confirmed 1D UTC candle."""
        self._require_connected()
        previous_signature = None
        attempt = 0
        while self._connected:
            try:
                payload = await self._call("GET", "/api/v5/market/candles", {
                    "instId": "BTC-USDT", "bar": "1Dutc", "limit": 2,
                })
                confirmed = [row for row in payload.get("data", []) if len(row) < 9 or str(row[8]) == "1"]
                if confirmed:
                    item = max(confirmed, key=lambda row: int(row[0]))
                    signature = (item[0], item[4], item[5])
                    if signature != previous_signature:
                        previous_signature = signature
                        yield Bar(
                            datetime.fromtimestamp(int(item[0]) / 1000, tz=timezone.utc).date(),
                            float(item[1]), float(item[2]), float(item[3]), float(item[4]), float(item[5]),
                        )
                attempt = 0
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                await self._alert_stream_failure(exc, attempt)
                delay = min(self.reconnect_max_seconds, 2 ** min(attempt - 1, 5))
                await asyncio.sleep(delay + random.random() * min(1.0, delay * 0.1))
