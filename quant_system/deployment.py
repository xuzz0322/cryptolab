"""Fail-closed Paper -> Testnet -> capped Live rollout gates."""

from dataclasses import dataclass
from enum import Enum
from typing import List


class RolloutStage(str, Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class DeploymentEvidence:
    paper_days: int = 0
    paper_orders: int = 0
    testnet_days: int = 0
    testnet_orders: int = 0
    max_drawdown: float = 0.0
    reconciliation_errors: int = 0
    consecutive_runtime_errors: int = 0
    requested_live_capital: float = 0.0
    manual_confirmation: str = ""
    verified: bool = False
    evidence_digest: str = ""


@dataclass
class DeploymentPolicy:
    minimum_paper_days: int = 14
    minimum_paper_orders: int = 20
    minimum_testnet_days: int = 7
    minimum_testnet_orders: int = 10
    maximum_drawdown: float = 0.10
    maximum_live_capital: float = 500.0
    live_confirmation_phrase: str = "I_UNDERSTAND_LIVE_TRADING_RISK"


@dataclass(frozen=True)
class GateDecision:
    approved: bool
    reasons: List[str]


class DeploymentGate:
    transitions = {
        RolloutStage.DRAFT: RolloutStage.VALIDATED,
        RolloutStage.VALIDATED: RolloutStage.PAPER,
        RolloutStage.PAPER: RolloutStage.TESTNET,
        RolloutStage.TESTNET: RolloutStage.LIVE,
    }

    def __init__(self, policy: DeploymentPolicy = None):
        self.policy = policy or DeploymentPolicy()

    def evaluate(
        self, current: RolloutStage, target: RolloutStage, evidence: DeploymentEvidence
    ) -> GateDecision:
        reasons: List[str] = []
        if target == RolloutStage.RETIRED:
            return GateDecision(True, ["允许随时下线策略版本"])
        if self.transitions.get(current) != target:
            reasons.append("只允许按 DRAFT→VALIDATED→PAPER→TESTNET→LIVE 顺序晋级")
            return GateDecision(False, reasons)
        if target in {RolloutStage.TESTNET, RolloutStage.LIVE}:
            if not evidence.verified or len(evidence.evidence_digest) != 64:
                reasons.append("部署证据必须由RuntimeEvidenceStore生成并带有效摘要")
        if evidence.reconciliation_errors:
            reasons.append("存在账本对账差异")
        if evidence.consecutive_runtime_errors:
            reasons.append("存在未清零的连续运行错误")
        if abs(evidence.max_drawdown) > self.policy.maximum_drawdown:
            reasons.append("阶段最大回撤超过 %.1f%%" % (self.policy.maximum_drawdown * 100))
        if target == RolloutStage.TESTNET:
            if evidence.paper_days < self.policy.minimum_paper_days:
                reasons.append("Paper运行天数不足")
            if evidence.paper_orders < self.policy.minimum_paper_orders:
                reasons.append("Paper订单样本不足")
        if target == RolloutStage.LIVE:
            if evidence.testnet_days < self.policy.minimum_testnet_days:
                reasons.append("Testnet运行天数不足")
            if evidence.testnet_orders < self.policy.minimum_testnet_orders:
                reasons.append("Testnet订单样本不足")
            if not 0 < evidence.requested_live_capital <= self.policy.maximum_live_capital:
                reasons.append("实盘资金必须在 (0, %.2f] 内" % self.policy.maximum_live_capital)
            if evidence.manual_confirmation != self.policy.live_confirmation_phrase:
                reasons.append("缺少实盘人工风险确认短语")
        return GateDecision(not reasons, reasons or ["部署门禁检查通过"])
