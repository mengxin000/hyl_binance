from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite


def default_fees() -> dict[str, dict[str, float]]:
    # Configurable base-tier assumptions; no account tier or discounts are queried.
    return {
        "binance_spot": {"maker": 10.0, "taker": 10.0},
        "binance_perp": {"maker": 2.0, "taker": 5.0},
        "hyperliquid_spot": {"maker": 4.0, "taker": 7.0},
        "hyperliquid_perp": {"maker": 1.5, "taker": 4.5},
    }


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    enabled: bool = True
    order_notional_usdt: float = 10.0
    exchange_delay_ms: float = 20.0
    deal_delay_ms: float = 30.0
    resolution_grace_ms: float = 5.0
    max_data_age_ms: float = 3000.0
    history_seconds: float = 15.0
    history_events_per_instrument: int = 5000
    window_samples: int = 500  # Resolved observations, including unavailable ones.
    min_samples: int = 5  # Required valid observations in EACH direction to score.
    fees_bps: dict[str, dict[str, float]] = field(default_factory=default_fees)

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled 必须是 JSON 布尔值")
        numbers = (
            self.order_notional_usdt,
            self.exchange_delay_ms,
            self.deal_delay_ms,
            self.resolution_grace_ms,
            self.max_data_age_ms,
            self.history_seconds,
        )
        if any(not isfinite(v) for v in numbers):
            raise ValueError("模拟参数必须是有限数值")
        if self.order_notional_usdt <= 0 or min(self.exchange_delay_ms, self.deal_delay_ms) < 0:
            raise ValueError("下单金额必须为正，延迟不能为负")
        if not 0 <= self.resolution_grace_ms <= 1000:
            raise ValueError("resolution_grace_ms 必须在 0~1000")
        if self.max_data_age_ms <= 0 or self.history_seconds * 1000 <= (
            self.max_data_age_ms + self.exchange_delay_ms + self.deal_delay_ms
        ):
            raise ValueError("history_seconds 必须覆盖行情年龄与两段延迟")
        if any(
            type(v) is not int
            for v in (self.min_samples, self.window_samples, self.history_events_per_instrument)
        ):
            raise ValueError("样本数与事件容量必须是整数")
        if self.min_samples < 1 or not 2 * self.min_samples <= self.window_samples <= 10000:
            raise ValueError("需要 min_samples >= 1，2 * min_samples <= window_samples <= 10000")
        if self.history_events_per_instrument < 100:
            raise ValueError("history_events_per_instrument 至少为 100")
        if not isinstance(self.fees_bps, dict) or set(self.fees_bps) != set(default_fees()):
            raise ValueError("fees_bps 必须提供四种交易所/市场类型的费率")
        for key in default_fees():
            fee = self.fees_bps.get(key, {})
            if set(fee) != {"maker", "taker"} or any(
                not isinstance(v, (int, float)) or not isfinite(v) for v in fee.values()
            ):
                raise ValueError(f"fees_bps.{key} 必须提供有限的 maker/taker bps 费率")


@dataclass(frozen=True, slots=True)
class ScoreConfig:
    enabled: bool = True
    profit_weight: float = 0.6
    slippage_weight: float = 0.4
    tail_weight: float = 0.5
    confidence_paired_samples: int = 30

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled 必须是 JSON 布尔值")
        values = (
            self.profit_weight,
            self.slippage_weight,
            self.tail_weight,
        )
        if any(not isfinite(v) for v in values):
            raise ValueError("评分参数必须是有限数值")
        if min(self.slippage_weight, self.profit_weight) < 0 or (
            self.slippage_weight + self.profit_weight <= 0
        ):
            raise ValueError("评分权重必须非负且合计大于零")
        if not 0 <= self.tail_weight <= 1:
            raise ValueError("tail_weight 需要在 0~1")
        if type(self.confidence_paired_samples) is not int or self.confidence_paired_samples < 1:
            raise ValueError("confidence_paired_samples 必须为正整数")
