from __future__ import annotations

from collections.abc import Mapping

from ..config.settings import MarketConfig
from .models import BookQuote, Category, Instrument, SpreadRoute, SpreadSnapshot

BASE_IDENTITY_MAX_PRICE_DEVIATION = 0.01


def _base_key(instrument: Instrument, aliases: Mapping[str, str]) -> str:
    return aliases.get(
        f"{instrument.venue}:{instrument.base}", aliases.get(instrument.base, instrument.base)
    ).upper()


def _match_base_keys(hyperliquid_base: str, binance_base: str) -> tuple[str, bool] | None:
    """Match exact bases, or propose a substring match for price validation."""
    hl_base, bn_base = hyperliquid_base.upper(), binance_base.upper()
    if hl_base == bn_base:
        return hl_base, False
    if min(len(hl_base), len(bn_base)) < 3:
        return None
    if hl_base in bn_base:
        return hl_base, True
    if bn_base in hl_base:
        return bn_base, True
    return None


def build_routes(
    instruments: tuple[Instrument, ...], config: MarketConfig
) -> tuple[SpreadRoute, ...]:
    aliases = config.base_aliases or {}
    eligible: dict[str, list[Instrument]] = {"hyperliquid": [], "binance": []}
    for item in instruments:
        # Stablecoin conversion pairs are internal price feeds, never arb routes.
        if item.base in config.quote_assets:
            continue
        minimum = getattr(config, f"{item.venue}_min_{item.kind}_volume_usdt")
        if item.quote_volume < minimum:
            continue
        eligible[item.venue].append(item)
    routes: list[SpreadRoute] = []
    # Pair by exact normalized base first, or by a contained symbol candidate.
    # Containment alone is not trusted: calculate_snapshot confirms that fresh,
    # quote-converted prices are within 1% before the route enters statistics.
    for left in eligible["hyperliquid"]:
        left_base = _base_key(left, aliases)
        for kind in ("spot", "perp"):
            binance_candidates = [
                item for item in eligible["binance"] if item.kind == kind
            ]
            exact_matches = [
                item for item in binance_candidates if _base_key(item, aliases) == left_base
            ]
            # An exact ticker is authoritative for that market type. Only try
            # containment when no exact Binance base exists for this leg kind.
            candidates = (
                [(item, (left_base, False)) for item in exact_matches]
                if exact_matches
                else [
                    (item, match)
                    for item in binance_candidates
                    if (match := _match_base_keys(left_base, _base_key(item, aliases)))
                    is not None
                ]
            )
            for right, (base, needs_validation) in candidates:
                category: Category = (
                    "spot_spot"
                    if left.kind == right.kind == "spot"
                    else "perp_perp"
                    if left.kind == right.kind == "perp"
                    else "spot_perp"
                )
                rid = f"{category}:{base}:{left.symbol}:{right.symbol}"
                routes.append(SpreadRoute(rid, category, base, left, right, needs_validation))
    return tuple(routes)


def conversion_rates(
    instruments: tuple[Instrument, ...], quotes: Mapping[str, BookQuote], now: float, max_age: float
) -> dict[tuple[str, str], float]:
    """Return the latest fresh Binance USDC->USDT conversion for both venues."""
    rates: dict[tuple[str, str], float] = {
        (venue, "USDT"): 1.0 for venue in ("hyperliquid", "binance")
    }
    latest: tuple[Instrument, BookQuote] | None = None
    for instrument in instruments:
        if (
            instrument.venue != "binance"
            or instrument.kind != "spot"
            or (instrument.base, instrument.quote) not in (("USDC", "USDT"), ("USDT", "USDC"))
        ):
            continue
        quote = quotes.get(instrument.key)
        if (
            quote is None
            or not 0 < now - quote.received_mono <= max_age
            or quote.bid <= 0
            or quote.ask <= 0
        ):
            continue
        if latest is None or quote.received_mono > latest[1].received_mono:
            latest = (instrument, quote)
    if latest is not None:
        instrument, quote = latest
        usdc_to_usdt = quote.mid if instrument.base == "USDC" else 1.0 / quote.mid
        rates[("hyperliquid", "USDC")] = usdc_to_usdt
        rates[("binance", "USDC")] = usdc_to_usdt
    return rates


def calculate_snapshot(
    route: SpreadRoute,
    quotes: Mapping[str, BookQuote],
    rates: Mapping[tuple[str, str], float],
    now: float,
    max_age: float,
) -> tuple[SpreadSnapshot | None, str]:
    hq, bq = quotes.get(route.hyperliquid.key), quotes.get(route.binance.key)
    if not hq or not bq:
        return None, "missing_quote"
    h_age, b_age = now - hq.received_mono, now - bq.received_mono
    if h_age < 0 or b_age < 0 or max(h_age, b_age) > max_age:
        return None, "stale_quote"
    skew = abs(hq.received_mono - bq.received_mono)
    # Arrival-time skew is diagnostic only: the venues publish BBO updates
    # on different schedules, so it is not a sound validity gate by itself.
    hrate, brate = (
        rates.get((route.hyperliquid.venue, route.hyperliquid.quote)),
        rates.get((route.binance.venue, route.binance.quote)),
    )
    if hrate is None or brate is None:
        return None, "missing_usdc_conversion"
    hm, bm = hq.mid * hrate, bq.mid * brate
    if hm <= 0 or bm <= 0:
        return None, "invalid_price"
    if (
        route.requires_base_price_validation
        and abs(hm / bm - 1.0) > BASE_IDENTITY_MAX_PRICE_DEVIATION
    ):
        return None, "base_price_mismatch"
    if route.category == "spot_perp":
        spot_instrument = route.hyperliquid if route.hyperliquid.kind == "spot" else route.binance
        perp_instrument = route.hyperliquid if route.hyperliquid.kind == "perp" else route.binance
        spot_quote = hq if spot_instrument is route.hyperliquid else bq
        perp_quote = hq if perp_instrument is route.hyperliquid else bq
        spot_rate = hrate if spot_instrument is route.hyperliquid else brate
        perp_rate = hrate if perp_instrument is route.hyperliquid else brate
        # Mixed spot/perp basis uses the explicitly requested executable side:
        # buy the perp at its ask and sell the spot at its bid.
        spread = (perp_quote.ask * perp_rate / (spot_quote.bid * spot_rate) - 1.0) * 10_000
    else:
        spread = (hm / bm - 1.0) * 10_000
    # Executable BBO directions for equal notional: buy one venue's ask,
    # sell the other venue's bid. In mixed routes these naturally mean
    # buying spot/selling perp or buying perp/selling spot as applicable.
    hl_buy_bn_sell = (bq.bid * brate / (hq.ask * hrate) - 1.0) * 10_000
    bn_buy_hl_sell = (hq.bid * hrate / (bq.ask * brate) - 1.0) * 10_000
    return SpreadSnapshot(
        route,
        hm,
        bm,
        spread,
        max(h_age, b_age) * 1000,
        skew * 1000,
        now,
        route.hyperliquid.funding_rate,
        route.binance.funding_rate,
        hl_buy_bn_sell,
        bn_buy_hl_sell,
        h_age * 1000,
        b_age * 1000,
    ), "ok"
