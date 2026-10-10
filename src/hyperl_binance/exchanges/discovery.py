from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

from ..config.settings import MarketConfig
from ..domain.models import Instrument
from .binance import _discover_binance
from .hyperliquid import _discover_hyperliquid


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    instruments: tuple[Instrument, ...]
    errors: dict[str, str]


class MarketMetadataAdapter(Protocol):
    """Normalize one venue's public market metadata to domain instruments."""

    def discover(self, config: MarketConfig) -> tuple[list[Instrument], dict[str, str]]: ...


class HyperliquidMetadataAdapter:
    def discover(self, config: MarketConfig) -> tuple[list[Instrument], dict[str, str]]:
        return _discover_hyperliquid(config)


class BinanceMetadataAdapter:
    def discover(self, config: MarketConfig) -> tuple[list[Instrument], dict[str, str]]:
        return _discover_binance(config)


def discover_markets(config: MarketConfig) -> DiscoveryResult:
    """Fetch each public venue independently, then combine normalized instruments."""
    adapters: tuple[MarketMetadataAdapter, ...] = (
        HyperliquidMetadataAdapter(),
        BinanceMetadataAdapter(),
    )
    with ThreadPoolExecutor(
        max_workers=len(adapters), thread_name_prefix="market-discovery"
    ) as pool:
        futures = [pool.submit(adapter.discover, config) for adapter in adapters]
        results = [future.result() for future in futures]
    instruments = tuple(item for rows, _errors in results for item in rows)
    errors = {
        key: value for _rows, adapter_errors in results for key, value in adapter_errors.items()
    }
    return DiscoveryResult(instruments, errors)
