from __future__ import annotations

from collections import Counter, deque
from statistics import fmean

import numpy as np


class ExpectationModel:
    def __init__(self, window: int) -> None:
        # One window of resolved observations, including unavailable ones.
        # Counts, edges, Profit and coverage always describe the same period.
        self.observations: deque[dict] = deque(maxlen=window)
        self.invalid_total = Counter()
        self.attempts = 0
        self.last_received_ns = 0
        self._metrics: dict | None = None

    def record(self, sample: dict) -> None:
        self.observations.append(sample)
        self.last_received_ns = sample["taker_target_ns"]
        self._metrics = None

    def record_unavailable(self, phase: str, reason: str) -> None:
        self.observations.append(
            {"type": "unavailable_observation", "phase": phase, "reason": reason}
        )
        self.invalid_total[reason] += 1
        self._metrics = None

    def view(self) -> dict:
        samples = [
            sample for sample in self.observations if sample["type"] == "execution_observation"
        ]
        invalid = Counter(
            sample["reason"]
            for sample in self.observations
            if sample["type"] == "unavailable_observation"
        )
        result = {
            "samples": len(samples),
            "attempts": self.attempts,
            "window_observations": len(self.observations),
            "invalid": sum(invalid.values()),
            "invalid_reasons": dict(invalid),
            "invalid_total": sum(self.invalid_total.values()),
            "coverage": len(samples) / len(self.observations) if self.observations else 0.0,
            "last_received_ns": self.last_received_ns,
        }
        if self._metrics is None:
            metrics = {}
            for field in (
                "delay_bps",
                "depth_bps",
                "total_slippage_bps",
                "fee_bps",
                "net_edge_bps",
                "cost_bps",
            ):
                metrics[f"mean_{field}"] = fmean(s[field] for s in samples) if samples else None
            for name, field in (
                ("p95_slippage_bps", "total_slippage_bps"),
                ("p95_cost_bps", "cost_bps"),
            ):
                metrics[name] = (
                    float(np.quantile([s[field] for s in samples], 0.95)) if samples else None
                )
            for phase in ("open", "close"):
                selected = [s for s in samples if s["phase"] == phase]
                metrics[f"{phase}_samples"] = len(selected)
                # Each observation uses the same configured USDT notional.
                # Thus equal-notional weighting is the arithmetic mean.
                metrics[f"mean_{phase}_net_edge_bps"] = (
                    fmean(s["net_edge_bps"] for s in selected) if selected else None
                )
                metrics[f"last_{phase}_received_ns"] = max(
                    (s["taker_target_ns"] for s in selected), default=0
                )
            paired = min(metrics["open_samples"], metrics["close_samples"])
            metrics["paired_samples"] = paired
            metrics["paired_edge_bps"] = (
                metrics["mean_open_net_edge_bps"] + metrics["mean_close_net_edge_bps"]
                if paired
                else None
            )
            metrics["profit_index"] = paired * metrics["paired_edge_bps"] if paired else None
            self._metrics = metrics
        result.update(self._metrics or {})
        return result
