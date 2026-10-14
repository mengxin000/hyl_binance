from __future__ import annotations

from ..domain.models import Instrument
from .config import SimulationConfig


class FeeModel:
    @staticmethod
    def charge(
        config: SimulationConfig,
        instrument: Instrument,
        role: str,
        quantity: float,
        price_usdt: float,
    ) -> float:
        rate = config.fees_bps[f"{instrument.venue}_{instrument.kind}"][role]
        return quantity * price_usdt * rate / 10000.0
