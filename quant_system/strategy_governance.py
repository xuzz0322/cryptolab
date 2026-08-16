"""Immutable strategy proposals, deterministic validation and human promotion."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .backtest import BacktestEngine
from .deployment import DeploymentEvidence, DeploymentGate, RolloutStage
from .models import BacktestConfig, Bar
from .strategies import create_strategy


@dataclass(frozen=True)
class StrategyProposal:
    strategy_name: str
    parameters: Dict[str, Any]
    rationale: str
    proposed_by: str = "AI"
    version_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            {"strategy_name": self.strategy_name, "parameters": self.parameters},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ValidationCriteria:
    minimum_bars: int = 120
    minimum_trades: int = 1
    minimum_sharpe: float = -0.25
    maximum_drawdown: float = 0.20


@dataclass(frozen=True)
class ValidationReport:
    version_id: str
    passed: bool
    failures: List[str]
    metrics: Dict[str, float]
    bars: int
    fingerprint: str
    validated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class StrategyVersionRecord:
    proposal: StrategyProposal
    stage: RolloutStage = RolloutStage.DRAFT
    validation: Optional[ValidationReport] = None
    audit_log: List[Dict[str, Any]] = field(default_factory=list)


class StrategyGovernanceService:
    """AI may propose/validate; only a human operator may promote versions."""

    def __init__(
        self,
        criteria: Optional[ValidationCriteria] = None,
        gate: Optional[DeploymentGate] = None,
        storage_path: Optional[Path] = None,
    ):
        self.criteria = criteria or ValidationCriteria()
        self.gate = gate or DeploymentGate()
        self.records: Dict[str, StrategyVersionRecord] = {}
        self.connection = None
        if storage_path is not None:
            path = Path(storage_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(path))
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS strategy_versions "
                "(version_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            self.connection.commit()
            self._load()

    def _payload(self, record: StrategyVersionRecord) -> str:
        return json.dumps(
            {
                "proposal": asdict(record.proposal),
                "stage": record.stage.value,
                "validation": asdict(record.validation) if record.validation else None,
                "audit_log": record.audit_log,
            },
            ensure_ascii=False,
        )

    def _persist(self, record: StrategyVersionRecord) -> None:
        if self.connection is None:
            return
        self.connection.execute(
            "INSERT OR REPLACE INTO strategy_versions(version_id, payload) VALUES (?, ?)",
            (record.proposal.version_id, self._payload(record)),
        )
        self.connection.commit()

    def _load(self) -> None:
        for row in self.connection.execute("SELECT payload FROM strategy_versions"):
            item = json.loads(row[0])
            proposal = StrategyProposal(**item["proposal"])
            validation = ValidationReport(**item["validation"]) if item["validation"] else None
            self.records[proposal.version_id] = StrategyVersionRecord(
                proposal, RolloutStage(item["stage"]), validation, item["audit_log"]
            )

    def propose(self, proposal: StrategyProposal) -> StrategyVersionRecord:
        # Constructor validation prevents un-runnable proposals from entering the registry.
        create_strategy(proposal.strategy_name, proposal.parameters)
        if any(record.proposal.fingerprint == proposal.fingerprint for record in self.records.values()):
            raise ValueError("相同策略参数版本已经存在")
        record = StrategyVersionRecord(proposal)
        record.audit_log.append({"action": "PROPOSED", "actor": proposal.proposed_by, "at": proposal.created_at})
        self.records[proposal.version_id] = record
        self._persist(record)
        return record

    def validate(
        self,
        version_id: str,
        bars: List[Bar],
        symbol: str = "BTC/USDT",
        actor: str = "AI_VALIDATOR",
    ) -> ValidationReport:
        record = self.get(version_id)
        failures: List[str] = []
        if len(bars) < self.criteria.minimum_bars:
            failures.append("验证行情数量不足")
            metrics: Dict[str, float] = {}
        else:
            # The last 40% is kept as chronological out-of-sample validation.
            validation_bars = bars[int(len(bars) * 0.60) :]
            if len(validation_bars) < 2:
                failures.append("样本外行情不足")
                metrics = {}
            else:
                strategy = create_strategy(
                    record.proposal.strategy_name, record.proposal.parameters
                )
                result = BacktestEngine(BacktestConfig()).run(
                    validation_bars, strategy, symbol=symbol
                )
                metrics = result.metrics
                if metrics.get("trade_count", 0) < self.criteria.minimum_trades:
                    failures.append("样本外交易数量不足")
                if metrics.get("sharpe_ratio", -999) < self.criteria.minimum_sharpe:
                    failures.append("样本外Sharpe低于门槛")
                if abs(metrics.get("max_drawdown", -1)) > self.criteria.maximum_drawdown:
                    failures.append("样本外最大回撤超过门槛")
        report = ValidationReport(
            version_id,
            not failures,
            failures,
            metrics,
            len(bars),
            record.proposal.fingerprint,
        )
        record.validation = report
        record.audit_log.append(
            {"action": "VALIDATED", "actor": actor, "passed": report.passed, "at": report.validated_at}
        )
        if not report.passed:
            self._persist(record)
            return report
        self._persist(record)
        return report

    def promote(
        self,
        version_id: str,
        target: RolloutStage,
        evidence: Optional[DeploymentEvidence] = None,
        actor_role: str = "HUMAN_OPERATOR",
    ) -> StrategyVersionRecord:
        if actor_role.upper().startswith("AI"):
            raise PermissionError("AI只能提案和验证，不能晋级或部署策略")
        record = self.get(version_id)
        if target == RolloutStage.VALIDATED and (not record.validation or not record.validation.passed):
            raise ValueError("策略版本尚未通过确定性验证")
        decision = self.gate.evaluate(record.stage, target, evidence or DeploymentEvidence())
        if not decision.approved:
            raise ValueError("部署门禁拒绝：" + "；".join(decision.reasons))
        previous = record.stage
        record.stage = target
        record.audit_log.append(
            {
                "action": "PROMOTED",
                "actor": actor_role,
                "from": previous.value,
                "to": target.value,
                "evidence": asdict(evidence or DeploymentEvidence()),
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._persist(record)
        return record

    def get(self, version_id: str) -> StrategyVersionRecord:
        if version_id not in self.records:
            raise ValueError("unknown strategy version: %s" % version_id)
        return self.records[version_id]

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
