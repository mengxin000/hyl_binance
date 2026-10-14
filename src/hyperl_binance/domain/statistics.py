from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from ..config.settings import StatisticsConfig
from .models import SpreadSnapshot
from .thresholds import TriggerLevels, resolve_thresholds


@dataclass(slots=True)
class DistributionStats:
    mean_bps: float = 0.0
    sigma_bps: float = 0.0
    low_bps: float = 0.0
    high_bps: float = 0.0
    p_low_bps: float = 0.0
    p_high_bps: float = 0.0
    p_low_long_bps: float = 0.0
    p_high_long_bps: float = 0.0
    p_low_short_bps: float = 0.0
    p_high_short_bps: float = 0.0
    sample_count: int = 0
    mean_long_bps: float = 0.0
    sigma_long_bps: float = 0.0
    mean_short_bps: float = 0.0
    sigma_short_bps: float = 0.0


class RouteHistory:
    def __init__(self, config: StatisticsConfig, quantile_refresh_seconds: float = 15.0) -> None:
        self.config = config
        self.quantile_refresh_seconds = quantile_refresh_seconds
        self.samples: deque[tuple[float, float]] = deque(maxlen=config.total_window_samples)
        self.current: SpreadSnapshot | None = None
        self.stats = DistributionStats()
        self._last_quantile = 0.0
        self._total_sum = self._total_sumsq = 0.0
        self._long: deque[float] = deque()
        self._short: deque[float] = deque()
        self._long_sum = self._long_sumsq = 0.0
        self._short_sum = self._short_sumsq = 0.0
        self._window_limits = (
            config.total_window_samples,
            config.long_window_samples,
            config.short_window_samples,
        )
        self._trigger_quantile_key: tuple | None = None
        self._trigger_quantiles: tuple[float, float, float] | None = None
        self._last_trigger_quantile = 0.0
        self.funding_samples: deque[tuple[float, float | None, float | None]] = deque(
            maxlen=31 * 24 + 2
        )

    def load_funding_samples(
        self, samples: list[tuple[float, float | None, float | None]], now: float
    ) -> None:
        cutoff = now - 30 * 24 * 60 * 60
        self.funding_samples.clear()
        self.funding_samples.extend(sample for sample in samples if cutoff <= sample[0] <= now)

    def add_funding_sample(
        self, sampled_at: float, funding_hl: float | None, funding_binance: float | None
    ) -> None:
        cutoff = sampled_at - 30 * 24 * 60 * 60
        while self.funding_samples and self.funding_samples[0][0] < cutoff:
            self.funding_samples.popleft()
        if self.funding_samples and sampled_at <= self.funding_samples[-1][0]:
            return
        self.funding_samples.append((sampled_at, funding_hl, funding_binance))

    def funding_mean_30d(self, now: float) -> tuple[float | None, float | None]:
        cutoff = now - 30 * 24 * 60 * 60
        samples = [sample for sample in self.funding_samples if cutoff <= sample[0] <= now]

        def average(index: int) -> float | None:
            observed = [
                (sample[0], sample[index + 1])
                for sample in samples
                if sample[index + 1] is not None
            ]
            # Do not call a partial startup history a 30-day mean. Hourly
            # observations allow a small amount of downtime, but require 90%
            # coverage and a sample near the beginning of the requested window.
            if len(observed) < 648 or not observed or observed[0][0] > cutoff + 90 * 60:
                return None
            return sum(value for _ts, value in observed) / len(observed)

        return average(0), average(1)

    def add(self, snapshot: SpreadSnapshot) -> None:
        self._sync_windows()
        self.current = snapshot
        if len(self.samples) == self.samples.maxlen:
            old = self.samples[0][1]
            self._total_sum -= old
            self._total_sumsq -= old * old
        self.samples.append((snapshot.sampled_at, snapshot.spread_bps))
        value = snapshot.spread_bps
        self._total_sum += value
        self._total_sumsq += value * value
        for window, limit, attr in (
            (self._long, self.config.long_window_samples, "long"),
            (self._short, self.config.short_window_samples, "short"),
        ):
            total_name, square_name = f"_{attr}_sum", f"_{attr}_sumsq"
            if len(window) == limit:
                removed = window[0]
                setattr(self, total_name, getattr(self, total_name) - removed)
                setattr(self, square_name, getattr(self, square_name) - removed * removed)
            window.append(value)
            setattr(self, total_name, getattr(self, total_name) + value)
            setattr(self, square_name, getattr(self, square_name) + value * value)
        self.stats.mean_bps, self.stats.sigma_bps = self._moments(
            self._total_sum, self._total_sumsq, len(self.samples)
        )
        self.stats.mean_long_bps, self.stats.sigma_long_bps = self._moments(
            self._long_sum, self._long_sumsq, len(self._long)
        )
        self.stats.mean_short_bps, self.stats.sigma_short_bps = self._moments(
            self._short_sum, self._short_sumsq, len(self._short)
        )
        if self.stats.sample_count == 0:
            self.stats.low_bps = self.stats.high_bps = value
        else:
            self.stats.low_bps, self.stats.high_bps = (
                min(self.stats.low_bps, value),
                max(self.stats.high_bps, value),
            )
        if snapshot.sampled_at - self._last_quantile >= self.quantile_refresh_seconds:
            vals = np.fromiter((v for _, v in self.samples), dtype=np.float64)
            self.stats.p_low_bps, self.stats.p_high_bps = (
                float(v)
                for v in np.quantile(vals, (self.config.quantile_low, self.config.quantile_high))
            )
            long_vals = np.fromiter(self._long, dtype=np.float64)
            short_vals = np.fromiter(self._short, dtype=np.float64)
            self.stats.p_low_long_bps, self.stats.p_high_long_bps = (
                float(v)
                for v in np.quantile(
                    long_vals, (self.config.quantile_low, self.config.quantile_high)
                )
            )
            self.stats.p_low_short_bps, self.stats.p_high_short_bps = (
                float(v)
                for v in np.quantile(
                    short_vals, (self.config.quantile_low, self.config.quantile_high)
                )
            )
            self._last_quantile = snapshot.sampled_at
        self.stats.sample_count = len(self.samples)

    def _sync_windows(self) -> None:
        limits = (
            self.config.total_window_samples,
            self.config.long_window_samples,
            self.config.short_window_samples,
        )
        if limits == self._window_limits:
            return
        self.samples = deque(list(self.samples)[-limits[0] :], maxlen=limits[0])
        values = [value for _ts, value in self.samples]
        self._total_sum, self._total_sumsq = sum(values), sum(v * v for v in values)
        for size, attr in ((limits[1], "long"), (limits[2], "short")):
            selected = values[-size:]
            setattr(self, f"_{attr}", deque(selected))
            setattr(self, f"_{attr}_sum", sum(selected))
            setattr(self, f"_{attr}_sumsq", sum(v * v for v in selected))
        self._window_limits = limits
        self._last_quantile = self._last_trigger_quantile = 0.0
        self._trigger_quantile_key = None

    def trigger_thresholds(self) -> TriggerLevels:
        """Publish route-specific thresholds without coupling to execution models."""
        self._sync_windows()
        if self.config.threshold_window == "total":
            count, total, squares = len(self.samples), self._total_sum, self._total_sumsq
        else:
            attr = self.config.threshold_window
            count = len(getattr(self, f"_{attr}"))
            total, squares = getattr(self, f"_{attr}_sum"), getattr(self, f"_{attr}_sumsq")
        mean, sigma = self._moments(total, squares, count)
        quantiles = None
        if self.config.threshold_mode != "quantile":
            self._trigger_quantile_key = None
        if self.config.threshold_mode == "quantile" and count >= self.config.threshold_min_samples:
            key = (
                self.config.threshold_window,
                getattr(self.config, f"{self.config.threshold_window}_window_samples"),
                self.config.close_threshold_quantile,
                self.config.open_threshold_quantile,
                self.config.threshold_min_samples,
                self.quantile_refresh_seconds,
            )
            now = self.current.sampled_at if self.current else 0.0
            if key != self._trigger_quantile_key or (
                now - self._last_trigger_quantile >= self.quantile_refresh_seconds
            ):
                values = (
                    [value for _ts, value in self.samples]
                    if self.config.threshold_window == "total"
                    else list(getattr(self, f"_{self.config.threshold_window}"))
                )
                self._trigger_quantiles = tuple(
                    float(value)
                    for value in np.quantile(
                        values,
                        (
                            self.config.close_threshold_quantile,
                            0.5,
                            self.config.open_threshold_quantile,
                        ),
                    )
                )
                self._trigger_quantile_key, self._last_trigger_quantile = key, now
            quantiles = self._trigger_quantiles
        return resolve_thresholds(self.config, count, mean, sigma, quantiles)

    @staticmethod
    def _moments(total: float, sumsq: float, count: int) -> tuple[float, float]:
        if not count:
            return 0.0, 0.0
        mean = total / count
        return mean, max(0.0, sumsq / count - mean * mean) ** 0.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.stats)
