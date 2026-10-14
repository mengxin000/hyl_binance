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
    opportunities: dict[str, int],
    config: ScoreConfig,
    min_samples: int,
    now_ns: int,
) -> dict[str, float]:
    """Relative score within one page; fees enter cost once, not again in score."""
    if not config.enabled:
        return {}
    eligible = {
        rid: result
        for rid, result in simulations.items()
        if rid in opportunities
        and result["samples"] >= min_samples
        and 0 <= now_ns - result["last_received_ns"] <= config.max_sample_age_seconds * 1e9
    }
    costs = {
        rid: (1 - config.tail_weight) * result["mean_cost_bps"]
        + config.tail_weight * result["p95_cost_bps"]
        for rid, result in eligible.items()
    }
    weight = config.slippage_weight + config.opportunity_weight
    scores = {}
    for rid, result in eligible.items():
        coverage = (result["attempts"] - result["invalid"] - int(result["pending"])) / max(
            1, result["attempts"] - int(result["pending"])
        )
        cost_score = _rank(costs[rid], list(costs.values()), lower_is_better=True)
        activity = _rank(opportunities[rid], [opportunities[k] for k in eligible])
        scores[rid] = (
            100
            * coverage
            * (config.slippage_weight * cost_score + config.opportunity_weight * activity)
            / weight
        )
    return scores
