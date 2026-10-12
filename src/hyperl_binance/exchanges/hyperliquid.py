from __future__ import annotations

import requests

from ..config.settings import MarketConfig
from ..domain.models import Instrument
from .http import hl_info, optional_float


def _discover_hyperliquid(config: MarketConfig) -> tuple[list[Instrument], dict[str, str]]:
    session = requests.Session()
    session.headers.update(
        {"Content-Type": "application/json", "User-Agent": "hyperl-binance-spread/0.1"}
    )
    instruments: list[Instrument] = []
    errors: dict[str, str] = {}
    try:
        spot_response = hl_info(session, {"type": "spotMetaAndAssetCtxs"})
        spot_meta, spot_contexts = spot_response
        tokens = {int(token["index"]): token for token in spot_meta.get("tokens", [])}
        names_to_ids: dict[str, set[str]] = {}
        for token in tokens.values():
            names_to_ids.setdefault(str(token.get("name", "")).upper(), set()).add(
                str(token.get("tokenId", ""))
            )
        for index, market in enumerate(spot_meta.get("universe", [])):
            try:
                indices = market.get("tokens", [])
                if len(indices) != 2:
                    continue
                base_token, quote_token = tokens[int(indices[0])], tokens[int(indices[1])]
                base = str(base_token["name"]).upper()
                quote = str(quote_token["name"]).upper()
                is_converter = (base, quote) in (("USDC", "USDT"), ("USDT", "USDC"))
                if quote not in config.quote_assets or (
                    base in config.quote_assets and not is_converter
                ):
                    continue
                if len(names_to_ids.get(base, ())) > 1:
                    errors[f"ambiguous:hyperliquid:spot:{base}"] = "同名基础代币对应多个 tokenId"
                    continue
                if len(names_to_ids.get(quote, ())) > 1:
                    continue
                raw_spot_index = market.get("index")
                spot_index = int(raw_spot_index if raw_spot_index is not None else index)
                # spot asset contexts are indexed by the spot market's `index`,
                # which can differ from its position in the universe array.
                context = spot_contexts[spot_index] if 0 <= spot_index < len(spot_contexts) else {}
                volume = float(context.get("dayNtlVlm", 0) or 0)
                symbol = str(market.get("name") or f"@{spot_index}")
                # Hyperliquid BBO subscriptions use the spot pair index for
                # almost every spot market. PURR/USDC is the named exception.
                feed_coin = "PURR/USDC" if (base, quote) == ("PURR", "USDC") else f"@{spot_index}"
                instruments.append(
                    Instrument(
                        venue="hyperliquid",
                        kind="spot",
                        symbol=symbol,
                        feed_coin=feed_coin,
                        base=base,
                        quote=quote,
                        quote_volume=volume,
                    )
                )
            except (KeyError, TypeError, ValueError, IndexError):
                errors[f"spot_row:{index}"] = "Hyperliquid 现货元数据行无法解析"
    except Exception as exc:
        errors["hyperliquid_spot_metadata"] = f"{type(exc).__name__}: {exc}"

    try:
        perp_response = hl_info(session, {"type": "metaAndAssetCtxs"})
        perp_meta, perp_contexts = perp_response
        overrides = config.hyperliquid_perp_quote_overrides or {}
        for index, market in enumerate(perp_meta.get("universe", [])):
            try:
                coin = str(market.get("name", "")).upper()
                if not coin or market.get("isDelisted") or ":" in coin:
                    continue
                # Hyperliquid perp metadata identifies the instrument by coin and
                # does not provide a quote asset. Normalize the default DEX perps
                # as USDC; use config overrides only for explicitly verified
                # exceptions. Never infer HIP-3 symbols.
                quote = str(overrides.get(coin, "USDC")).upper()
                if quote not in config.quote_assets:
                    continue
                base = coin[:-4] if coin.endswith("-USD") else coin
                if not base or base in config.quote_assets:
                    continue
                context = perp_contexts[index] if index < len(perp_contexts) else {}
                volume = float(context.get("dayNtlVlm", 0) or 0)
                rate = optional_float(context.get("funding"))
                instruments.append(
                    Instrument(
                        venue="hyperliquid",
                        kind="perp",
                        symbol=coin,
                        feed_coin=coin,
                        base=base,
                        quote=quote,
                        quote_volume=volume,
                        funding_rate=rate,
                    )
                )
            except (KeyError, TypeError, ValueError, IndexError):
                errors[f"perp_row:{index}"] = "Hyperliquid 永续元数据行无法解析"
    except Exception as exc:
        errors["hyperliquid_perp_metadata"] = f"{type(exc).__name__}: {exc}"
    session.close()
    return instruments, errors
