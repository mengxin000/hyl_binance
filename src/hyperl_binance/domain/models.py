from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Venue = Literal["hyperliquid", "binance"]
MarketKind = Literal["spot", "perp"]
Category = Literal["spot_spot", "spot_perp", "perp_perp"]


@dataclass(frozen=True, slots=True)
class Instrument:
    venue: Venue
    kind: MarketKind
    symbol: str
    feed_coin: str
    base: str
    quote: str
    quote_volume: float
    funding_rate: float | None = None
    funding_period_hours: float | None = None

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.kind}:{self.symbol}"


@dataclass(frozen=True, slots=True)
class BookQuote:
    bid: float
    ask: float
    received_mono: float
    received_wall: float
    event_time_ms: int | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True, slots=True)
class SpreadRoute:
    route_id: str
    category: Category
    base: str
    hyperliquid: Instrument
    binance: Instrument
    requires_base_price_validation: bool = False

    @property
    def symbols(self) -> tuple[str, str]:
        return self.hyperliquid.symbol, self.binance.symbol


@dataclass(frozen=True, slots=True)
class SpreadSnapshot:
    route: SpreadRoute
    hyperliquid_mid_usdt: float
    binance_mid_usdt: float
    spread_bps: float
    age_ms: float
    receive_skew_ms: float
    sampled_at: float
    funding_hl: float | None
    funding_binance: float | None
    hl_buy_bn_sell_bps: float
    bn_buy_hl_sell_bps: float
    hyperliquid_age_ms: float
    binance_age_ms: float
