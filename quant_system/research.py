"""Pre-registered strategy research with multiple-testing and overfit controls."""

import argparse
import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, replace
from itertools import combinations
from pathlib import Path
from statistics import NormalDist
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .backtest import BacktestEngine, BacktestResult
from .data import load_csv
from .models import BacktestConfig, Bar
from .strategies import create_strategy


NORMAL = NormalDist()


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / (len(values) - 1))


def period_sharpe(returns: Sequence[float]) -> float:
    deviation = _std(returns)
    return _mean(returns) / deviation if deviation else 0.0


def annual_sharpe(returns: Sequence[float], periods_per_year: int = 365) -> float:
    return period_sharpe(returns) * math.sqrt(periods_per_year)


def _moments(returns: Sequence[float]) -> Tuple[float, float]:
    if len(returns) < 3:
        return 0.0, 3.0
    average, deviation = _mean(returns), _std(returns)
    if not deviation:
        return 0.0, 3.0
    skew = _mean([((value - average) / deviation) ** 3 for value in returns])
    kurtosis = _mean([((value - average) / deviation) ** 4 for value in returns])
    return skew, kurtosis


def probabilistic_sharpe_ratio(returns: Sequence[float], benchmark_period_sharpe: float = 0.0) -> float:
    if len(returns) < 3:
        return 0.0
    sharpe = period_sharpe(returns)
    skew, kurtosis = _moments(returns)
    variance_term = 1 - skew * sharpe + ((kurtosis - 1) / 4) * sharpe * sharpe
    standard_error = math.sqrt(max(variance_term, 1e-12) / (len(returns) - 1))
    return NORMAL.cdf((sharpe - benchmark_period_sharpe) / standard_error)


def deflated_sharpe_ratio(
    selected_returns: Sequence[float], trial_period_sharpes: Sequence[float]
) -> Tuple[float, float]:
    """Return DSR and its luck-adjusted per-period Sharpe benchmark."""
    trials = max(1, len(trial_period_sharpes))
    if trials <= 1:
        benchmark = 0.0
    else:
        trial_variance = _std(trial_period_sharpes) ** 2
        gamma = 0.5772156649015329
        first = NORMAL.inv_cdf(1 - 1 / trials)
        second = NORMAL.inv_cdf(1 - 1 / (trials * math.e))
        benchmark = math.sqrt(max(trial_variance, 0.0)) * ((1 - gamma) * first + gamma * second)
    return probabilistic_sharpe_ratio(selected_returns, benchmark), benchmark


def minimum_track_record_length(
    returns: Sequence[float], confidence: float = 0.95, benchmark_period_sharpe: float = 0.0
) -> Optional[int]:
    sharpe = period_sharpe(returns)
    if len(returns) < 3 or sharpe <= benchmark_period_sharpe:
        return None
    skew, kurtosis = _moments(returns)
    variance_term = 1 - skew * sharpe + ((kurtosis - 1) / 4) * sharpe * sharpe
    required = 1 + max(variance_term, 1e-12) * (
        NORMAL.inv_cdf(confidence) / (sharpe - benchmark_period_sharpe)
    ) ** 2
    return int(math.ceil(required))


def holm_haircut(
    trial_returns: Mapping[str, Sequence[float]], selected_key: str, periods_per_year: int = 365
) -> Tuple[float, float, float]:
    """Return adjusted annual Sharpe, haircut fraction and adjusted one-sided p-value."""
    rows = []
    for key, returns in trial_returns.items():
        statistic = period_sharpe(returns) * math.sqrt(max(len(returns), 1))
        rows.append((max(0.0, min(1.0, 1 - NORMAL.cdf(statistic))), key))
    rows.sort()
    adjusted_so_far = 0.0
    selected_adjusted = 1.0
    total = len(rows)
    for index, (p_value, key) in enumerate(rows):
        adjusted_so_far = max(adjusted_so_far, min(1.0, p_value * (total - index)))
        if key == selected_key:
            selected_adjusted = adjusted_so_far
            break
    selected = trial_returns[selected_key]
    observed = annual_sharpe(selected, periods_per_year)
    if not selected or selected_adjusted >= 0.5:
        adjusted = 0.0
    else:
        adjusted = NORMAL.inv_cdf(1 - selected_adjusted) / math.sqrt(len(selected))
        adjusted *= math.sqrt(periods_per_year)
        adjusted = max(0.0, min(observed, adjusted))
    haircut = 1.0 if observed <= 0 else max(0.0, min(1.0, 1 - adjusted / observed))
    return adjusted, haircut, selected_adjusted


def probability_of_backtest_overfitting(
    trials_matrix: Sequence[Sequence[float]], n_blocks: int = 8
) -> Optional[float]:
    """CSCV PBO. Returns None when the matrix is too small for a meaningful estimate."""
    if not trials_matrix or len(trials_matrix[0]) < 10:
        return None
    width = len(trials_matrix[0])
    if any(len(row) != width for row in trials_matrix):
        raise ValueError("trials matrix must be rectangular")
    usable_blocks = min(n_blocks, len(trials_matrix))
    if usable_blocks % 2:
        usable_blocks -= 1
    if usable_blocks < 4:
        return None
    block_size = len(trials_matrix) // usable_blocks
    if block_size < 2:
        return None
    blocks = [list(range(i * block_size, (i + 1) * block_size)) for i in range(usable_blocks)]
    below_median = 0
    splits = 0
    all_blocks = set(range(usable_blocks))
    for train_blocks in combinations(range(usable_blocks), usable_blocks // 2):
        # Complement pairs are symmetric; retaining both is intentional CSCV enumeration.
        train_rows = [row for block in train_blocks for row in blocks[block]]
        test_rows = [row for block in sorted(all_blocks - set(train_blocks)) for row in blocks[block]]
        train_scores = [period_sharpe([trials_matrix[row][col] for row in train_rows]) for col in range(width)]
        selected = max(range(width), key=lambda col: (train_scores[col], -col))
        test_scores = [period_sharpe([trials_matrix[row][col] for row in test_rows]) for col in range(width)]
        selected_score = test_scores[selected]
        rank = 1 + sum(score < selected_score for score in test_scores)
        relative_rank = rank / (width + 1)
        if relative_rank <= 0.5:
            below_median += 1
        splits += 1
    return below_median / splits if splits else None


@dataclass(frozen=True)
class CandidateSpec:
    strategy_name: str
    parameters: Dict[str, Any]
    label: str = ""

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            {"strategy_name": self.strategy_name, "parameters": self.parameters},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def key(self) -> str:
        return self.label or "%s-%s" % (self.strategy_name, self.fingerprint[:10])


@dataclass
class ResearchPolicy:
    train_fraction: float = 0.50
    validation_fraction: float = 0.25
    periods_per_year: int = 365
    minimum_test_sharpe: float = 0.50
    maximum_test_drawdown: float = 0.15
    minimum_dsr: float = 0.95
    maximum_pbo: float = 0.50
    minimum_haircut_sharpe: float = 0.50
    minimum_candidates_for_pbo: int = 10
    cscv_blocks: int = 8
    walk_forward_window: int = 90
    minimum_profitable_walk_forward_fraction: float = 0.60
    cost_multipliers: Tuple[float, ...] = (1.0, 1.5, 2.0)
    minimum_stressed_sharpe: float = 0.0
    minimum_cross_assets: int = 0
    historical_test_is_pristine: bool = True

    def validate(self) -> None:
        if not 0 < self.train_fraction < 1 or not 0 < self.validation_fraction < 1:
            raise ValueError("split fractions must be in (0, 1)")
        if self.train_fraction + self.validation_fraction >= 1:
            raise ValueError("train + validation fractions must leave a final test set")
        if self.periods_per_year <= 0 or self.walk_forward_window < 2:
            raise ValueError("annualization and walk-forward window must be positive")
        if self.minimum_candidates_for_pbo < 10:
            raise ValueError("PBO requires at least 10 pre-registered candidates")
        if not self.cost_multipliers or min(self.cost_multipliers) < 1:
            raise ValueError("cost multipliers must be >= 1")


@dataclass
class ResearchReport:
    passed: bool
    failures: List[str]
    warnings: List[str]
    primary_symbol: str
    observations: int
    split: Dict[str, Any]
    selected_key: str
    selected_strategy_name: str
    selected_parameters: Dict[str, Any]
    selected_fingerprint: str
    candidate_count: int
    validation_sharpes: Dict[str, float]
    test_metrics: Dict[str, float]
    dsr: float
    dsr_benchmark_annual_sharpe: float
    pbo: Optional[float]
    haircut_sharpe: float
    haircut_fraction: float
    holm_adjusted_p_value: float
    minimum_track_record_length: Optional[int]
    cost_stress: Dict[str, Dict[str, float]]
    cross_asset: Dict[str, Dict[str, float]]
    walk_forward: List[Dict[str, Any]]
    artifacts: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _returns(result: BacktestResult, start: int, end: int) -> List[float]:
    equities = [point.equity for point in result.equity_curve]
    start = max(1, start)
    end = min(end, len(equities))
    return [equities[index] / equities[index - 1] - 1 for index in range(start, end)]


def _drawdown(returns: Sequence[float]) -> float:
    equity = peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1)
    return worst


def _segment_metrics(returns: Sequence[float], periods_per_year: int) -> Dict[str, float]:
    equity = 1.0
    for value in returns:
        equity *= 1 + value
    return {
        "total_return": equity - 1,
        "annual_sharpe": annual_sharpe(returns, periods_per_year),
        "max_drawdown": _drawdown(returns),
        "observations": float(len(returns)),
    }


class StrategyResearchPipeline:
    def __init__(self, policy: Optional[ResearchPolicy] = None, backtest_config: Optional[BacktestConfig] = None):
        self.policy = policy or ResearchPolicy()
        self.policy.validate()
        self.backtest_config = backtest_config or BacktestConfig()

    def run(
        self,
        primary_symbol: str,
        datasets: Mapping[str, Sequence[Bar]],
        candidates: Sequence[CandidateSpec],
        output_dir: Path,
    ) -> ResearchReport:
        if primary_symbol not in datasets:
            raise ValueError("primary symbol is missing from datasets")
        if not candidates:
            raise ValueError("at least one pre-registered candidate is required")
        keys = [candidate.key for candidate in candidates]
        if len(set(keys)) != len(keys) or len({candidate.fingerprint for candidate in candidates}) != len(candidates):
            raise ValueError("candidate labels and strategy fingerprints must be unique")
        bars = list(datasets[primary_symbol])
        if len(bars) < 120:
            raise ValueError("research pipeline requires at least 120 chronological bars")
        train_end = int(len(bars) * self.policy.train_fraction)
        validation_end = int(len(bars) * (self.policy.train_fraction + self.policy.validation_fraction))
        if min(train_end, validation_end - train_end, len(bars) - validation_end) < 20:
            raise ValueError("each chronological split needs at least 20 bars")

        results: Dict[str, BacktestResult] = {}
        validation_returns: Dict[str, List[float]] = {}
        test_returns: Dict[str, List[float]] = {}
        audit_returns: Dict[str, List[float]] = {}
        for candidate in candidates:
            result = BacktestEngine(self.backtest_config).run(
                bars, create_strategy(candidate.strategy_name, candidate.parameters), symbol=primary_symbol
            )
            results[candidate.key] = result
            validation_returns[candidate.key] = _returns(result, train_end, validation_end)
            test_returns[candidate.key] = _returns(result, validation_end, len(bars))
            audit_returns[candidate.key] = _returns(result, train_end, len(bars))
        validation_sharpes = {
            key: annual_sharpe(values, self.policy.periods_per_year)
            for key, values in validation_returns.items()
        }
        selected = max(candidates, key=lambda item: (validation_sharpes[item.key], item.fingerprint))
        selected_test = test_returns[selected.key]
        test_metrics = _segment_metrics(selected_test, self.policy.periods_per_year)
        trial_sharpes = [period_sharpe(test_returns[item.key]) for item in candidates]
        dsr, dsr_benchmark = deflated_sharpe_ratio(selected_test, trial_sharpes)
        haircut_sharpe, haircut_fraction, adjusted_p = holm_haircut(
            test_returns, selected.key, self.policy.periods_per_year
        )
        min_trl = minimum_track_record_length(selected_test)
        matrix = [[audit_returns[item.key][row] for item in candidates] for row in range(len(audit_returns[selected.key]))]
        pbo = probability_of_backtest_overfitting(matrix, self.policy.cscv_blocks)

        cost_stress: Dict[str, Dict[str, float]] = {}
        for multiplier in self.policy.cost_multipliers:
            config = replace(
                self.backtest_config,
                commission_rate=self.backtest_config.commission_rate * multiplier,
                slippage_rate=self.backtest_config.slippage_rate * multiplier,
                maker_fee_rate=self.backtest_config.maker_fee_rate * multiplier,
                taker_fee_rate=self.backtest_config.taker_fee_rate * multiplier,
            )
            stressed = BacktestEngine(config).run(
                bars, create_strategy(selected.strategy_name, selected.parameters), symbol=primary_symbol
            )
            cost_stress["%.2fx" % multiplier] = _segment_metrics(
                _returns(stressed, validation_end, len(bars)), self.policy.periods_per_year
            )

        cross_asset: Dict[str, Dict[str, float]] = {}
        for symbol, asset_bars_source in datasets.items():
            if symbol == primary_symbol:
                continue
            asset_bars = list(asset_bars_source)
            asset_test_start = int(len(asset_bars) * (self.policy.train_fraction + self.policy.validation_fraction))
            if len(asset_bars) < 120 or len(asset_bars) - asset_test_start < 20:
                cross_asset[symbol] = {"error": "insufficient bars"}
                continue
            result = BacktestEngine(self.backtest_config).run(
                asset_bars, create_strategy(selected.strategy_name, selected.parameters), symbol=symbol
            )
            cross_asset[symbol] = _segment_metrics(
                _returns(result, asset_test_start, len(asset_bars)), self.policy.periods_per_year
            )

        walk_forward = []
        for start in range(validation_end, len(bars), self.policy.walk_forward_window):
            end = min(len(bars), start + self.policy.walk_forward_window)
            values = _returns(results[selected.key], start, end)
            metrics = _segment_metrics(values, self.policy.periods_per_year)
            walk_forward.append({
                "start": bars[start].date.isoformat(),
                "end": bars[end - 1].date.isoformat(),
                **metrics,
            })

        failures: List[str] = []
        warnings: List[str] = []
        if not self.policy.historical_test_is_pristine:
            warnings.append(
                "历史最终测试区间已被v1研究查看，v2历史报告只能作为统计审计，"
                "真正最终证据依赖2026-08-15之后的前向数据"
            )
        if test_metrics["annual_sharpe"] < self.policy.minimum_test_sharpe:
            failures.append("最终测试集Sharpe低于门槛")
        if abs(test_metrics["max_drawdown"]) > self.policy.maximum_test_drawdown:
            failures.append("最终测试集最大回撤超过门槛")
        if dsr < self.policy.minimum_dsr:
            failures.append("Deflated Sharpe Ratio低于门槛")
        if len(candidates) < self.policy.minimum_candidates_for_pbo or pbo is None:
            failures.append("候选不足10个，无法形成有效PBO证据")
        elif pbo > self.policy.maximum_pbo:
            failures.append("Probability of Backtest Overfitting超过门槛")
        if haircut_sharpe < self.policy.minimum_haircut_sharpe:
            failures.append("Holm多重检验折损后Sharpe低于门槛")
        if min_trl is None or min_trl > len(selected_test):
            failures.append("实际最终测试记录短于Minimum Track Record Length")
        if min(item["annual_sharpe"] for item in cost_stress.values()) < self.policy.minimum_stressed_sharpe:
            failures.append("成本压力测试失败")
        profitable_folds = sum(item["total_return"] > 0 for item in walk_forward)
        profitable_fraction = profitable_folds / len(walk_forward) if walk_forward else 0.0
        if profitable_fraction < self.policy.minimum_profitable_walk_forward_fraction:
            failures.append("Walk-forward盈利窗口比例不足")
        valid_cross_assets = [item for item in cross_asset.values() if "error" not in item]
        if len(valid_cross_assets) < self.policy.minimum_cross_assets:
            failures.append("跨币种验证数量不足")
        elif valid_cross_assets and any(item["annual_sharpe"] <= 0 for item in valid_cross_assets):
            warnings.append("至少一个跨币种测试Sharpe不为正")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        selected_path = output_dir / "selected_returns.csv"
        with selected_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "return"])
            for index, value in enumerate(selected_test, start=validation_end):
                writer.writerow([bars[index].date.isoformat(), "%.16g" % value])
        matrix_path = output_dir / "trials_matrix.csv"
        with matrix_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date"] + keys)
            for offset, row in enumerate(matrix, start=train_end):
                writer.writerow([bars[offset].date.isoformat()] + ["%.16g" % value for value in row])

        report = ResearchReport(
            passed=not failures,
            failures=failures,
            warnings=warnings,
            primary_symbol=primary_symbol,
            observations=len(bars),
            split={
                "train": [bars[0].date.isoformat(), bars[train_end - 1].date.isoformat()],
                "validation": [bars[train_end].date.isoformat(), bars[validation_end - 1].date.isoformat()],
                "test": [bars[validation_end].date.isoformat(), bars[-1].date.isoformat()],
            },
            selected_key=selected.key,
            selected_strategy_name=selected.strategy_name,
            selected_parameters=selected.parameters,
            selected_fingerprint=selected.fingerprint,
            candidate_count=len(candidates),
            validation_sharpes=validation_sharpes,
            test_metrics=test_metrics,
            dsr=dsr,
            dsr_benchmark_annual_sharpe=dsr_benchmark * math.sqrt(self.policy.periods_per_year),
            pbo=pbo,
            haircut_sharpe=haircut_sharpe,
            haircut_fraction=haircut_fraction,
            holm_adjusted_p_value=adjusted_p,
            minimum_track_record_length=min_trl,
            cost_stress=cost_stress,
            cross_asset=cross_asset,
            walk_forward=walk_forward,
            artifacts={"selected_returns": str(selected_path), "trials_matrix": str(matrix_path)},
        )
        report_path = output_dir / "research_report.json"
        report.artifacts["report"] = str(report_path)
        report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return report


def _config(path: Path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    datasets = {symbol: load_csv(Path(source)) for symbol, source in payload["datasets"].items()}
    candidates = [CandidateSpec(**item) for item in payload["candidates"]]
    policy_values = dict(payload.get("policy", {}))
    if "cost_multipliers" in policy_values:
        policy_values["cost_multipliers"] = tuple(policy_values["cost_multipliers"])
    policy = ResearchPolicy(**policy_values)
    backtest = BacktestConfig(**payload.get("backtest_config", {}))
    return payload, datasets, candidates, policy, backtest


def main() -> None:
    parser = argparse.ArgumentParser(description="严格时间切分、Walk-forward和回测过拟合验证")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    payload, datasets, candidates, policy, backtest = _config(args.config)
    report = StrategyResearchPipeline(policy, backtest).run(
        payload["primary_symbol"], datasets, candidates, args.output_dir
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if not report.passed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
