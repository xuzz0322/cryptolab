"""Dependency-free Binance Spot Testnet REST adapter.

The adapter defaults to trading disabled and refuses a production base URL
unless allow_live=True is passed explicitly.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import random
import time
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..exchange import (
    AdapterNotConnectedError,
    AuthenticationError,
    Balance,
    ExchangeAdapter,
    ExchangeError,
    ExchangePosition,
    TradingDisabledError,
    ExchangeUserEvent,
    UserEventKind,
)
from ..alerts import Alert, AlertManager, AlertSeverity
from ..rate_limit import AsyncExchangeRateLimiter
from ..instruments import INSTRUMENTS, Instrument, get_instrument
from ..models import Bar
from ..order_models import ACTIVE_STATUSES, Fill, Order, OrderStatus, OrderType, Side


HttpTransport = Callable[[str, str, Dict[str, str], Optional[bytes], float], Any]
WebSocketConnector = Callable[[str], Any]


def _http_json(method: str, url: str, headers: Dict[str, str], data: Optional[bytes], timeout: float) -> Any:
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            message = payload.get("msg", "HTTP %s" % exc.code)
        except Exception:
            message = "HTTP %s" % exc.code
        raise ExchangeError("Binance API error: %s" % message) from exc
    except Exception as exc:
        raise ExchangeError("Binance connection failed: %s" % exc) from exc


async def _websocket_connect(url: str):
    try:
        import websockets
    except ImportError as exc:
        raise ExchangeError("实时用户流需要安装可选依赖：pip install '.[live]'") from exc
    return await websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=10)


class BinanceSpotTestnetAdapter(ExchangeAdapter):
    venue = "BINANCE"
    testnet_url = "https://testnet.binance.vision"
    production_url = "https://api.binance.com"

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: str = testnet_url,
        trading_enabled: bool = False,
        allow_live: bool = False,
        timeout: float = 15.0,
        transport: HttpTransport = _http_json,
        websocket_connector: WebSocketConnector = _websocket_connect,
        rate_limiter: Optional[AsyncExchangeRateLimiter] = None,
        alert_manager: Optional[AlertManager] = None,
        listen_key_keepalive_seconds: float = 1_800.0,
        reconnect_max_seconds: float = 30.0,
    ):
        normalized_url = base_url.rstrip("/")
        if normalized_url == self.production_url and not allow_live:
            raise ValueError("生产环境必须显式设置 allow_live=True")
        self.api_key = api_key or os.environ.get("BINANCE_TESTNET_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("BINANCE_TESTNET_API_SECRET", "")
        self.base_url = normalized_url
        self.allow_live = allow_live
        self.trading_enabled = trading_enabled
        self.timeout = timeout
        self.transport = transport
        self.websocket_connector = websocket_connector
        self.rate_limiter = rate_limiter or AsyncExchangeRateLimiter()
        self.alert_manager = alert_manager or AlertManager()
        self.listen_key_keepalive_seconds = listen_key_keepalive_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self._connected = False
        self._kill_switch = False
        self._time_offset_ms = 0
        self._orders: Dict[str, Order] = {}
        self._listen_key: Optional[str] = None
        self._user_socket = None

    @property
    def connected(self) -> bool:
        return self._connected

    def _require_connected(self) -> None:
        if not self._connected:
            raise AdapterNotConnectedError("Binance adapter is not connected")

    def _require_auth(self) -> None:
        if not self.api_key or not self.api_secret:
            raise AuthenticationError("缺少 BINANCE_TESTNET_API_KEY/API_SECRET")

    def _require_api_key(self) -> None:
        if not self.api_key:
            raise AuthenticationError("缺少 BINANCE_TESTNET_API_KEY")

    @staticmethod
    def _request_weight(path: str, method: str, params: Dict[str, Any]) -> int:
        if path == "/api/v3/account":
            return 20
        if path == "/api/v3/openOrders":
            return 6 if params.get("symbol") else 80
        if path == "/api/v3/myTrades":
            return 20
        return 1

    async def _call(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        api_key_only: bool = False,
    ) -> Any:
        values = dict(params or {})
        is_order = path == "/api/v3/order" and method in {"POST", "DELETE"}
        await self.rate_limiter.acquire(self._request_weight(path, method, values), is_order=is_order)
        headers = {"Accept": "application/json", "User-Agent": "CryptoLab/0.2"}
        if signed:
            self._require_auth()
            values["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            values["recvWindow"] = 5000
            query_to_sign = urlencode(values)
            values["signature"] = hmac.new(
                self.api_secret.encode("utf-8"), query_to_sign.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            headers["X-MBX-APIKEY"] = self.api_key
        elif api_key_only:
            self._require_api_key()
            headers["X-MBX-APIKEY"] = self.api_key
        query = urlencode(values)
        data = None
        url = self.base_url + path
        if method in ("GET", "DELETE"):
            if query:
                url += "?" + query
        else:
            data = query.encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.transport(method, url, headers, data, self.timeout)
        )

    async def connect(self) -> None:
        payload = await self._call("GET", "/api/v3/time")
        server_time = int(payload["serverTime"])
        self._time_offset_ms = server_time - int(time.time() * 1000)
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        if self._user_socket is not None:
            with contextlib.suppress(Exception):
                await self._user_socket.close()
            self._user_socket = None
        if self._listen_key and self.api_key:
            with contextlib.suppress(Exception):
                await self.close_listen_key(self._listen_key)
        self._listen_key = None

    async def set_kill_switch(self, enabled: bool, cancel_open_orders: bool = True) -> None:
        self._kill_switch = enabled
        if enabled and cancel_open_orders and self._connected and self.api_key and self.api_secret:
            for order in list(await self.get_open_orders()):
                try:
                    await self.cancel_order(order.order_id, order.symbol)
                except ExchangeError:
                    pass

    @staticmethod
    def _exchange_symbol(symbol: str) -> str:
        return symbol.upper().replace("/", "").replace("-", "").replace("_", "")

    @staticmethod
    def _number(value: float) -> str:
        return ("%.16f" % value).rstrip("0").rstrip(".")

    @staticmethod
    def _status(value: str) -> OrderStatus:
        return {
            "NEW": OrderStatus.ACCEPTED,
            "PENDING_NEW": OrderStatus.SUBMITTED,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.REJECTED,
            "EXPIRED_IN_MATCH": OrderStatus.REJECTED,
        }.get(value, OrderStatus.SUBMITTED)

    def _update_order(self, order: Order, payload: Dict[str, Any]) -> Order:
        order.exchange_order_id = str(payload.get("orderId", order.exchange_order_id or "")) or None
        order.status = self._status(str(payload.get("status", "NEW")))
        order.filled_quantity = float(payload.get("executedQty", order.filled_quantity))
        quote = float(payload.get("cummulativeQuoteQty", 0) or 0)
        if order.filled_quantity > 0 and quote > 0:
            order.average_fill_price = quote / order.filled_quantity
        if order.status == OrderStatus.REJECTED:
            order.reject_reason = str(payload.get("msg", "交易所拒单"))
        self._orders[order.order_id] = order
        return order

    async def submit_order(self, order: Order) -> Order:
        self._require_connected()
        if not self.trading_enabled:
            raise TradingDisabledError("交易开关未开启；请仅在测试网显式设置 trading_enabled=True")
        if self._kill_switch:
            raise TradingDisabledError("Kill Switch 已开启，禁止下单")
        instrument = get_instrument(order.symbol)
        quantity = instrument.normalize_quantity(order.quantity)
        if quantity < float(instrument.minimum_quantity):
            raise ValueError("订单数量低于交易所最小值")
        params: Dict[str, Any] = {
            "symbol": self._exchange_symbol(order.symbol),
            "side": order.side.value,
            "type": order.order_type.value,
            "quantity": self._number(quantity),
            "newClientOrderId": order.client_order_id,
            "newOrderRespType": "FULL",
        }
        if order.order_type == OrderType.LIMIT:
            if order.limit_price is None:
                raise ValueError("限价单缺少 limit_price")
            params["price"] = self._number(instrument.normalize_price(order.limit_price))
            params["timeInForce"] = "GTC"
        order.status = OrderStatus.SUBMITTED
        self._orders[order.order_id] = order
        payload = await self._call("POST", "/api/v3/order", params, signed=True)
        return self._update_order(order, payload)

    def _local_order(self, order_id: str) -> Optional[Order]:
        if order_id in self._orders:
            return self._orders[order_id]
        return next((item for item in self._orders.values() if item.exchange_order_id == str(order_id)), None)

    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Order:
        self._require_connected()
        order = self._local_order(order_id)
        target_symbol = symbol or (order.symbol if order else None)
        if not target_symbol:
            raise ValueError("cancel_order requires symbol for an unknown local order")
        params: Dict[str, Any] = {"symbol": self._exchange_symbol(target_symbol)}
        if order:
            params["origClientOrderId"] = order.client_order_id
        else:
            params["orderId"] = order_id
        payload = await self._call("DELETE", "/api/v3/order", params, signed=True)
        if order is None:
            order = self._order_from_payload(payload, target_symbol)
        return self._update_order(order, payload)

    async def get_order(self, order_id: str, symbol: Optional[str] = None) -> Optional[Order]:
        self._require_connected()
        order = self._local_order(order_id)
        target_symbol = symbol or (order.symbol if order else None)
        if not target_symbol:
            return None
        params: Dict[str, Any] = {"symbol": self._exchange_symbol(target_symbol)}
        if order:
            params["origClientOrderId"] = order.client_order_id
        else:
            params["orderId"] = order_id
        payload = await self._call("GET", "/api/v3/order", params, signed=True)
        if order is None:
            order = self._order_from_payload(payload, target_symbol)
        return self._update_order(order, payload)

    def _order_from_payload(self, payload: Dict[str, Any], symbol: str) -> Order:
        timestamp = int(payload.get("time", payload.get("transactTime", int(time.time() * 1000))))
        return Order(
            symbol=symbol,
            side=Side(str(payload.get("side", "BUY"))),
            quantity=float(payload.get("origQty", 0)),
            trading_date=datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).date(),
            order_type=OrderType(str(payload.get("type", "MARKET"))),
            limit_price=float(payload["price"]) if float(payload.get("price", 0) or 0) else None,
            client_order_id=str(payload.get("clientOrderId", "")),
            exchange_order_id=str(payload.get("orderId", "")) or None,
        )

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Order]:
        self._require_connected()
        params = {"symbol": self._exchange_symbol(symbol)} if symbol else {}
        payload = await self._call("GET", "/api/v3/openOrders", params, signed=True)
        result = []
        for item in payload:
            local = next(
                (order for order in self._orders.values() if order.client_order_id == item.get("clientOrderId")), None
            )
            order = local or self._order_from_payload(item, symbol or str(item["symbol"]))
            result.append(self._update_order(order, item))
        return result

    async def get_balances(self) -> List[Balance]:
        self._require_connected()
        payload = await self._call("GET", "/api/v3/account", signed=True)
        return [
            Balance(str(item["asset"]), float(item["free"]), float(item["locked"]))
            for item in payload.get("balances", [])
            if float(item["free"]) or float(item["locked"])
        ]

    async def get_positions(self) -> List[ExchangePosition]:
        balances = {item.currency: item for item in await self.get_balances()}
        result = []
        for instrument in INSTRUMENTS.values():
            balance = balances.get(instrument.base_currency)
            if balance and balance.total > 0:
                result.append(
                    ExchangePosition(instrument.symbol, balance.total, balance.free)
                )
        return result

    async def get_fills(self, symbol: Optional[str] = None) -> List[Fill]:
        self._require_connected()
        if not symbol:
            raise ValueError("Binance myTrades 查询必须指定 symbol")
        payload = await self._call(
            "GET", "/api/v3/myTrades", {"symbol": self._exchange_symbol(symbol), "limit": 1000}, signed=True
        )
        return [
            Fill(
                order_id=str(item["orderId"]),
                symbol=symbol,
                side=Side.BUY if item.get("isBuyer") else Side.SELL,
                quantity=float(item["qty"]),
                price=float(item["price"]),
                commission=float(item["commission"]),
                trading_date=datetime.fromtimestamp(int(item["time"]) / 1000, tz=timezone.utc).date(),
                fill_id=str(item["id"]),
            )
            for item in payload
        ]

    async def get_instruments(self) -> Sequence[Instrument]:
        return [item for item in INSTRUMENTS.values() if item.venue == self.venue]

    async def create_listen_key(self) -> str:
        self._require_connected()
        payload = await self._call(
            "POST", "/api/v3/userDataStream", api_key_only=True
        )
        key = str(payload.get("listenKey", ""))
        if not key:
            raise ExchangeError("交易所未返回Listen Key")
        self._listen_key = key
        return key

    async def keepalive_listen_key(self, listen_key: Optional[str] = None) -> None:
        key = listen_key or self._listen_key
        if not key:
            raise ExchangeError("Listen Key尚未创建")
        await self._call(
            "PUT", "/api/v3/userDataStream", {"listenKey": key}, api_key_only=True
        )

    async def close_listen_key(self, listen_key: Optional[str] = None) -> None:
        key = listen_key or self._listen_key
        if not key:
            return
        await self._call(
            "DELETE", "/api/v3/userDataStream", {"listenKey": key}, api_key_only=True
        )
        if key == self._listen_key:
            self._listen_key = None

    @property
    def user_stream_base_url(self) -> str:
        if self.base_url == self.production_url:
            return "wss://stream.binance.com:9443/ws"
        return "wss://stream.testnet.binance.vision/ws"

    async def _renew_listen_key(self, key: str) -> None:
        while self._connected and self._listen_key == key:
            await asyncio.sleep(self.listen_key_keepalive_seconds)
            try:
                await self.keepalive_listen_key(key)
            except Exception:
                self._listen_key = None
                raise

    def _symbol_from_exchange(self, value: str) -> str:
        return next(
            (
                instrument.symbol
                for instrument in INSTRUMENTS.values()
                if self._exchange_symbol(instrument.symbol) == value
            ),
            value,
        )

    def _translate_user_payload(self, payload: Dict[str, Any]) -> Optional[ExchangeUserEvent]:
        event_type = str(payload.get("e", ""))
        event_time = datetime.fromtimestamp(
            int(payload.get("E", int(time.time() * 1000))) / 1000, tz=timezone.utc
        )
        if event_type == "listenKeyExpired":
            self._listen_key = None
            return ExchangeUserEvent(
                UserEventKind.LISTEN_KEY_EXPIRED,
                self.venue,
                raw_event_type=event_type,
                event_time=event_time,
            )
        if event_type == "outboundAccountPosition":
            balances = [
                Balance(str(item["a"]), float(item["f"]), float(item["l"]))
                for item in payload.get("B", [])
            ]
            return ExchangeUserEvent(
                UserEventKind.BALANCE,
                self.venue,
                balances=balances,
                raw_event_type=event_type,
                event_time=event_time,
            )
        if event_type == "balanceUpdate":
            return ExchangeUserEvent(
                UserEventKind.BALANCE,
                self.venue,
                balances=[Balance(str(payload["a"]), float(payload["d"]))],
                raw_event_type=event_type,
                event_time=event_time,
            )
        if event_type != "executionReport":
            return None
        symbol = self._symbol_from_exchange(str(payload["s"]))
        client_id = str(payload.get("c", ""))
        order = next(
            (item for item in self._orders.values() if item.client_order_id == client_id), None
        )
        if order is None:
            order = Order(
                symbol=symbol,
                side=Side(str(payload["S"])),
                quantity=float(payload["q"]),
                trading_date=event_time.date(),
                order_type=OrderType(str(payload["o"])),
                limit_price=float(payload["p"]) if float(payload.get("p", 0) or 0) else None,
                client_order_id=client_id,
                exchange_order_id=str(payload.get("i", "")) or None,
            )
        normalized = {
            "orderId": payload.get("i"),
            "status": payload.get("X"),
            "executedQty": payload.get("z", 0),
            "cummulativeQuoteQty": payload.get("Z", 0),
        }
        self._update_order(order, normalized)
        last_quantity = float(payload.get("l", 0) or 0)
        fill = None
        if last_quantity > 0:
            fill = Fill(
                order_id=order.order_id,
                symbol=symbol,
                side=order.side,
                quantity=last_quantity,
                price=float(payload.get("L", 0)),
                commission=float(payload.get("n", 0) or 0),
                trading_date=event_time.date(),
                fill_id=str(payload.get("t", "%s-%s" % (order.order_id, payload.get("T", 0)))),
            )
        return ExchangeUserEvent(
            UserEventKind.TRADE if fill else UserEventKind.ORDER,
            self.venue,
            order=order,
            fill=fill,
            raw_event_type=event_type,
            event_time=event_time,
        )

    async def _alert_stream_failure(self, error: Exception, attempt: int) -> None:
        await self.alert_manager.emit(
            Alert(
                "Binance用户流断开",
                "用户流将在退避后自动重连",
                AlertSeverity.WARNING if attempt < 5 else AlertSeverity.CRITICAL,
                "BinanceSpotTestnetAdapter",
                {"venue": self.venue, "error": str(error), "count": attempt},
            ),
            dedupe_key="binance-user-stream-disconnect-%d" % min(attempt, 5),
        )

    async def stream_user_events(self) -> AsyncIterator[ExchangeUserEvent]:
        self._require_connected()
        attempt = 0
        while self._connected:
            renew_task = None
            socket = None
            try:
                key = self._listen_key or await self.create_listen_key()
                socket = await self.websocket_connector(self.user_stream_base_url + "/" + key)
                self._user_socket = socket
                renew_task = asyncio.create_task(self._renew_listen_key(key))
                while self._connected:
                    receive_task = asyncio.create_task(socket.recv())
                    done, _ = await asyncio.wait(
                        {receive_task, renew_task}, timeout=90, return_when=asyncio.FIRST_COMPLETED
                    )
                    if not done:
                        receive_task.cancel()
                        raise ExchangeError("用户流90秒未收到数据")
                    if renew_task in done:
                        receive_task.cancel()
                        await renew_task
                        raise ExchangeError("Listen Key续期任务意外结束")
                    raw = await receive_task
                    event = self._translate_user_payload(json.loads(raw))
                    if event is not None:
                        attempt = 0
                        yield event
                    if event and event.kind == UserEventKind.LISTEN_KEY_EXPIRED:
                        raise ExchangeError("Listen Key已失效")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                await self._alert_stream_failure(exc, attempt)
                delay = min(self.reconnect_max_seconds, 2 ** min(attempt - 1, 5))
                await asyncio.sleep(delay + random.random() * min(1.0, delay * 0.1))
            finally:
                if renew_task:
                    renew_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await renew_task
                if socket is not None:
                    with contextlib.suppress(Exception):
                        await socket.close()
                self._user_socket = None

    async def stream_bars(self) -> AsyncIterator[Bar]:
        """Poll only the latest *closed* UTC daily candle to avoid look-ahead/leakage."""
        self._require_connected()
        previous_signature = None
        attempt = 0
        while self._connected:
            try:
                payload = await self._call(
                    "GET", "/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1d", "limit": 2}
                )
                attempt = 0
                if len(payload) >= 2:
                    item = payload[-2]
                    signature = (item[0], item[4], item[5])
                    if signature != previous_signature:
                        previous_signature = signature
                        yield Bar(
                            datetime.fromtimestamp(int(item[0]) / 1000, tz=timezone.utc).date(),
                            float(item[1]), float(item[2]), float(item[3]), float(item[4]), float(item[5]),
                        )
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                await self._alert_stream_failure(exc, attempt)
                delay = min(self.reconnect_max_seconds, 2 ** min(attempt - 1, 5))
                await asyncio.sleep(delay + random.random() * min(1.0, delay * 0.1))
