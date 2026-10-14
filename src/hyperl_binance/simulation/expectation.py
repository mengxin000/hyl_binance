from __future__ import annotations

from collections import Counter, deque
from statistics import fmean

import numpy as np


class ExpectationModel:
    def __init__(self, window: int) -> None:
        self.samples: deque[dict] = deque(maxlen=window)
        self.invalid = Counter()
        self.attempts = 0
        self.last_received_ns = 0
        self._metrics: dict | None = None

    def record(self, sample: dict) -> None:
        self.samples.append(sample)
        self.last_received_ns = sample["taker_target_ns"]
        self._metrics = None

    def view(self) -> dict:
        samples = list(self.samples)
        result = {
            "samples": len(samples),
            "attempts": self.attempts,
            "invalid": sum(self.invalid.values()),
            "invalid_reasons": dict(self.invalid),
            "last_received_ns": self.last_received_ns,
        }
        if samples and self._metrics is None:
            metrics = {}
            for field in (
                "delay_bps",
                "depth_bps",
                "total_slippage_bps",
                "fee_bps",
                "net_edge_bps",
                "cost_bps",
            ):
                metrics[f"mean_{field}"] = fmean(s[field] for s in samples)
            metrics["p95_slippage_bps"] = float(
                np.quantile([s["total_slippage_bps"] for s in samples], 0.95)
            )
            metrics["p95_cost_bps"] = float(np.quantile([s["cost_bps"] for s in samples], 0.95))
            metrics["open_samples"] = sum(s["phase"] == "open" for s in samples)
            metrics["close_samples"] = len(samples) - metrics["open_samples"]
            self._metrics = metrics
        result.update(self._metrics or {})
        return result
