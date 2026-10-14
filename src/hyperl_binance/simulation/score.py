from __future__ import annotations

from .config import ScoreConfig


def _rank(value: float, values: list[float], lower_is_better: bool = False) -> float:
    if len(values) == 1:
        return 0.5
    worse = sum(v > value if lower_is_better else v < value for v in values)
    tied = sum(v == value for v in values)
    return (worse + (tied - 1) / 2) / (len(values) - 1)


def score_routes(
    simulations: dict[str, dict],
    route_ids: set[str],
    config: ScoreConfig,
    min_samples: int,
    now_ns: int,
) -> dict[str, float]:
    """Rank paired net edge and execution risk using simulation observations only."""
    if not config.enabled:
        return {}
    eligible = {
        rid: result
        for rid, result in simulations.items()
        if rid in route_ids
        and result.get("paired_samples", 0) >= min_samples
        and all(
            0 <= now_ns - result[f"last_{phase}_received_ns"] <= config.max_sample_age_seconds * 1e9
            for phase in ("open", "close")
        )
    }
    profitable = {rid: result for rid, result in eligible.items() if result["profit_index"] > 0}
    risks = {
        rid: (1 - config.tail_weight) * result["mean_total_slippage_bps"]
        + config.tail_weight * result["p95_slippage_bps"]
        for rid, result in profitable.items()
    }
    profits = [result["profit_index"] for result in profitable.values()]
    weight = config.slippage_weight + config.profit_weight
    scores = {}
    for rid, result in eligible.items():
        # A negative paired net edge cannot gain a high score from low slippage.
        if rid not in profitable:
            scores[rid] = 0.0
            continue
        coverage = result["coverage"]
        confidence = min(1.0, result["paired_samples"] / config.confidence_paired_samples)
        risk_score = _rank(risks[rid], list(risks.values()), lower_is_better=True)
        profit_score = _rank(result["profit_index"], profits)
        scores[rid] = (
            100
            * coverage
            * confidence
            * (config.slippage_weight * risk_score + config.profit_weight * profit_score)
            / weight
        )
    return scores
