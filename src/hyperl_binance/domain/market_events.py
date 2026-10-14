from __future__ import annotations

from dataclasses import dataclass

from .models import Instrument


@dataclass(frozen=True, slots=True)
class QuoteEvent:
    instrument: Instrument
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float
    received_ns: int
    source_ms: int | None = None


@dataclass(frozen=True, slots=True)
class DepthEvent:
    instrument: Instrument
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    received_ns: int
    source_ms: int | None = None


@dataclass(frozen=True, slots=True)
class StreamReset:
    keys: tuple[str, ...]
    received_ns: int


MarketEvent = QuoteEvent | DepthEvent | StreamReset
