"""External alert sinks with deduplication and secret-safe payloads."""

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from urllib.request import Request, urlopen
from uuid import uuid4


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Alert:
    title: str
    message: str
    severity: AlertSeverity
    source: str
    details: Dict[str, Any] = field(default_factory=dict)
    alert_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def safe_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["severity"] = self.severity.value
        def redact(value):
            if not isinstance(value, str):
                return value
            value = re.sub(r"(?:https?|wss?)://\S+", "[REDACTED_URL]", value)
            value = re.sub(
                r"(?i)(api[_ -]?key|secret|listen[_ -]?key)(\s*[:=]\s*)\S+",
                r"\1\2[REDACTED]",
                value,
            )
            return value[:1000]
        result["message"] = redact(result["message"])
        # Details are allow-listed by key to prevent accidental credential leakage.
        allowed = {"venue", "symbol", "order_id", "error", "stage", "discrepancies", "count"}
        result["details"] = {
            key: redact(value) for key, value in self.details.items() if key in allowed
        }
        return result


class AlertSink(ABC):
    @abstractmethod
    async def send(self, alert: Alert) -> None: ...


WebhookTransport = Callable[[str, bytes, float], None]


def _post_webhook(url: str, body: bytes, timeout: float) -> None:
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        response.read()


class WebhookAlertSink(AlertSink):
    def __init__(
        self,
        url: str,
        timeout: float = 10.0,
        transport: WebhookTransport = _post_webhook,
        max_attempts: int = 3,
    ):
        if not url.startswith("https://"):
            raise ValueError("alert webhook must use HTTPS")
        self.url, self.timeout, self.transport = url, timeout, transport
        self.max_attempts = max_attempts

    async def send(self, alert: Alert) -> None:
        body = json.dumps(alert.safe_dict(), ensure_ascii=False).encode("utf-8")
        loop = asyncio.get_running_loop()
        last_error = None
        for attempt in range(self.max_attempts):
            try:
                await loop.run_in_executor(None, lambda: self.transport(self.url, body, self.timeout))
                return
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    await asyncio.sleep(min(2 ** attempt, 5))
        raise last_error


class MemoryAlertSink(AlertSink):
    def __init__(self):
        self.alerts: List[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


class AlertManager:
    def __init__(self, sinks: Optional[List[AlertSink]] = None, cooldown_seconds: float = 60.0):
        self.sinks = sinks or []
        self.cooldown_seconds = cooldown_seconds
        self.last_sent: Dict[str, float] = {}
        self.history: List[Alert] = []

    async def emit(self, alert: Alert, dedupe_key: Optional[str] = None) -> bool:
        key = dedupe_key or "%s:%s:%s" % (alert.source, alert.severity.value, alert.title)
        now = asyncio.get_running_loop().time()
        if now - self.last_sent.get(key, -1e30) < self.cooldown_seconds:
            return False
        self.last_sent[key] = now
        self.history.append(alert)
        if self.sinks:
            results = await asyncio.gather(
                *(sink.send(alert) for sink in self.sinks), return_exceptions=True
            )
            if all(isinstance(result, Exception) for result in results):
                raise RuntimeError("all external alert sinks failed")
        return True
