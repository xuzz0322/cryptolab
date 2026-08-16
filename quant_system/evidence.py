"""Durable rollout evidence collected from runtime events, not operator claims."""

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from .deployment import DeploymentEvidence, RolloutStage


class RuntimeEvidenceStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS runtime_evidence ("
            "event_id TEXT PRIMARY KEY, stage TEXT NOT NULL, kind TEXT NOT NULL, "
            "event_time TEXT NOT NULL, trading_date TEXT NOT NULL, value REAL NOT NULL, "
            "payload TEXT NOT NULL)"
        )
        self.connection.commit()

    def record(
        self,
        stage: RolloutStage,
        kind: str,
        trading_date: date,
        value: float = 0.0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO runtime_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                stage.value,
                kind,
                datetime.now(timezone.utc).isoformat(),
                trading_date.isoformat(),
                value,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        self.connection.commit()

    def record_heartbeat(self, stage: RolloutStage, trading_date: date, equity: float, drawdown: float) -> None:
        self.record(stage, "HEARTBEAT", trading_date, drawdown, {"equity": equity})

    def record_order(self, stage: RolloutStage, trading_date: date, order_id: str) -> None:
        self.record(stage, "ORDER", trading_date, 1.0, {"order_id": order_id})

    def record_reconciliation_error(self, stage: RolloutStage, trading_date: date, count: int) -> None:
        self.record(stage, "RECONCILIATION_ERROR", trading_date, float(count))

    def record_runtime_error(self, stage: RolloutStage, trading_date: date, error: str) -> None:
        self.record(stage, "RUNTIME_ERROR", trading_date, 1.0, {"error": error[:500]})

    def build_evidence(
        self,
        stage: RolloutStage,
        requested_live_capital: float = 0.0,
        manual_confirmation: str = "",
    ) -> DeploymentEvidence:
        rows = list(
            self.connection.execute(
                "SELECT event_id, kind, event_time, trading_date, value, payload "
                "FROM runtime_evidence WHERE stage = ? ORDER BY event_time, event_id",
                (stage.value,),
            )
        )
        if not rows:
            raise ValueError("没有可用于部署门禁的运行证据")
        days = len({row[3] for row in rows if row[1] == "HEARTBEAT"})
        # One order can produce SUBMITTED/ACCEPTED/PARTIALLY_FILLED/FILLED updates.
        # Deployment gates must count distinct orders, not state transitions.
        order_ids = set()
        for row in rows:
            if row[1] != "ORDER":
                continue
            try:
                order_id = str(json.loads(row[5]).get("order_id", ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                order_id = ""
            if order_id:
                order_ids.add(order_id)
        orders = len(order_ids)
        drawdowns = [float(row[4]) for row in rows if row[1] == "HEARTBEAT"]
        reconciliation_errors = sum(
            int(row[4]) for row in rows if row[1] == "RECONCILIATION_ERROR"
        )
        runtime_errors = 0
        for row in rows:
            if row[1] == "RUNTIME_ERROR":
                runtime_errors += 1
            elif row[1] == "HEARTBEAT":
                runtime_errors = 0
        canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        values = {
            "max_drawdown": min(drawdowns, default=0.0),
            "reconciliation_errors": reconciliation_errors,
            "consecutive_runtime_errors": runtime_errors,
            "requested_live_capital": requested_live_capital,
            "manual_confirmation": manual_confirmation,
            "verified": True,
            "evidence_digest": digest,
        }
        if stage == RolloutStage.PAPER:
            values.update({"paper_days": days, "paper_orders": orders})
        elif stage == RolloutStage.TESTNET:
            values.update({"testnet_days": days, "testnet_orders": orders})
        else:
            raise ValueError("只允许从PAPER或TESTNET运行记录生成晋级证据")
        return DeploymentEvidence(**values)

    def close(self) -> None:
        self.connection.close()
