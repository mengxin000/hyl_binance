from __future__ import annotations

from dataclasses import dataclass

from .config import SimulationConfig


@dataclass(frozen=True, slots=True)
class ExecutionDeadlines:
    maker_ns: int
    taker_ns: int


class LatencyModel:
    @staticmethod
    def targets(signal_ns: int, config: SimulationConfig) -> ExecutionDeadlines:
        maker_ns = signal_ns + int(config.exchange_delay_ms * 1e6)
        return ExecutionDeadlines(maker_ns, maker_ns + int(config.deal_delay_ms * 1e6))
