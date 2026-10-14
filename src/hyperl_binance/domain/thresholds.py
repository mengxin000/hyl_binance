from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import isfinite

from ..config.settings import StatisticsConfig


@dataclass(frozen=True, slots=True)
class TriggerLevels:
    """Immutable absolute levels; execution does not know how statistics were built."""

    mode: str
    window: str
    rule_id: str
    sample_count: int
    min_samples: int
    mean_bps: float
    rearm_bps: float
    open_bps: float | None
    close_bps: float | None
    status: str

    @property
    def ready(self) -> bool:
        return self.status == "ready"


def threshold_rule(config: StatisticsConfig) -> dict:
    rule = {
        "mode": config.threshold_mode,
        "window": config.threshold_window,
        "window_samples": getattr(config, f"{config.threshold_window}_window_samples"),
        "min_samples": 2 if config.threshold_mode == "fixed" else config.threshold_min_samples,
    }
    suffix = {"fixed": "bps", "sigma": "sigma", "quantile": "quantile"}[config.threshold_mode]
    rule["open"] = float(getattr(config, f"open_threshold_{suffix}"))
    rule["close"] = float(getattr(config, f"close_threshold_{suffix}"))
    return rule


def resolve_thresholds(
    config: StatisticsConfig,
    sample_count: int,
    mean: float,
    sigma: float,
    quantiles: tuple[float, float, float] | None = None,
) -> TriggerLevels:
    rule = threshold_rule(config)
    rule_id = hashlib.sha256(json.dumps(rule, sort_keys=True).encode()).hexdigest()[:12]
    center, upper, lower = mean, None, None
    status = "warming_up"
    if sample_count >= rule["min_samples"]:
        if config.threshold_mode == "fixed":
            upper, lower = mean + config.open_threshold_bps, mean + config.close_threshold_bps
        elif config.threshold_mode == "sigma":
            upper = mean + config.open_threshold_sigma * sigma
            lower = mean - config.close_threshold_sigma * sigma
        elif quantiles is not None:
            lower, center, upper = quantiles
        # In quantile mode the median is the rearm line. The mean need not lie
        # between the selected tail quantiles in an asymmetric distribution.
        status = "flat_distribution"
        if (
            upper is not None
            and lower is not None
            and (all(isfinite(v) for v in (mean, center, lower, upper)) and lower < center < upper)
        ):
            status = "ready"
        else:
            upper, lower = None, None
    return TriggerLevels(
        config.threshold_mode,
        config.threshold_window,
        rule_id,
        sample_count,
        rule["min_samples"],
        mean,
        center,
        upper,
        lower,
        status,
    )
