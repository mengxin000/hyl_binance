from __future__ import annotations

import requests

from ..config.settings import MarketConfig
from ..domain.models import Instrument
from .http import BINANCE_FUTURES_HOSTS, BINANCE_SPOT_HOSTS, get_binance_json, optional_float


def _discover_binance(config: MarketConfig) -> tuple[list[Instrument], dict[str, str]]:
    session = requests.Session()
    session.headers.update({"User-Agent": "hyperl-binance-spread/0.1"})
    instruments: list[Instrument] = []
    errors: dict[str, str] = {}

    try:
        symbols = get_binance_json(session, BINANCE_SPOT_HOSTS, "/api/v3/exchangeInfo").get(
            "symbols", []
        )
        tickers = get_binance_json(session, BINANCE_SPOT_HOSTS, "/api/v3/ticker/24hr")
        volume_by_symbol = {
            str(row.get("symbol", "")): optional_float(row.get("quoteVolume")) or 0.0
            for row in tickers
        }
        for row in symbols:
            base = str(row.get("baseAsset", "")).upper()
            quote = str(row.get("quoteAsset", "")).upper()
            symbol = str(row.get("symbol", ""))
            is_converter = (base, quote) in (("USDC", "USDT"), ("USDT", "USDC"))
            if (
                row.get("status") != "TRADING"
                or quote not in config.quote_assets
                or (base in config.quote_assets and not is_converter)
            ):
                continue
            instruments.append(
                Instrument(
                    venue="binance",
                    kind="spot",
                    symbol=symbol,
                    feed_coin=symbol,
                    base=base,
                    quote=quote,
                    quote_volume=volume_by_symbol.get(symbol, 0.0),
                )
            )
    except Exception as exc:
        errors["binance_spot_metadata"] = f"{type(exc).__name__}: {exc}"

    try:
        symbols = get_binance_json(session, BINANCE_FUTURES_HOSTS, "/fapi/v1/exchangeInfo").get(
            "symbols", []
        )
        tickers = get_binance_json(session, BINANCE_FUTURES_HOSTS, "/fapi/v1/ticker/24hr")
        volume_by_symbol = {
            str(row.get("symbol", "")): optional_float(row.get("quoteVolume")) or 0.0
            for row in tickers
        }
        funding_by_symbol: dict[str, float] = {}
        try:
            premium = get_binance_json(session, BINANCE_FUTURES_HOSTS, "/fapi/v1/premiumIndex")
            funding_by_symbol = {
                str(row.get("symbol", "")): optional_float(row.get("lastFundingRate"))
                for row in premium
                if optional_float(row.get("lastFundingRate")) is not None
            }
        except Exception as exc:
            errors["binance_funding"] = f"{type(exc).__name__}: {exc}"
        for row in symbols:
            base = str(row.get("baseAsset", "")).upper()
            quote = str(row.get("quoteAsset", "")).upper()
            symbol = str(row.get("symbol", ""))
            if (
                row.get("status") != "TRADING"
                or row.get("contractType") != "PERPETUAL"
                or quote not in config.quote_assets
                or base in config.quote_assets
            ):
                continue
            instruments.append(
                Instrument(
                    venue="binance",
                    kind="perp",
                    symbol=symbol,
                    feed_coin=symbol,
                    base=base,
                    quote=quote,
                    quote_volume=volume_by_symbol.get(symbol, 0.0),
                    funding_rate=funding_by_symbol.get(symbol),
                )
            )
    except Exception as exc:
        errors["binance_perp_metadata"] = f"{type(exc).__name__}: {exc}"
    session.close()
    return instruments, errors
