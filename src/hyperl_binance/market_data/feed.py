from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from collections.abc import Callable
from math import isfinite
from typing import Any

from ..domain.market_events import DepthEvent, MarketEvent, QuoteEvent, StreamReset
from ..domain.models import BookQuote, Instrument
from .binance_stream import BinanceBookTickerStream
from .hyperliquid_stream import HyperliquidBboStream


class PublicFeed:
    """Independent async WS thread; publish only latest quote per instrument."""

    def __init__(
        self,
        symbols_per_connection: int,
        hl_subscriptions_per_connection: int,
        coalesce_ms: int,
        event_sink: Callable[[MarketEvent], None] | None = None,
    ) -> None:
        self.symbols_per_connection = symbols_per_connection
        self.hl_subscriptions_per_connection = hl_subscriptions_per_connection
        self.coalesce_seconds = coalesce_ms / 1000
        self.event_sink = event_sink
        self.depth_enabled = False
        self.depth_keys: frozenset[str] = frozenset()
        self._quotes: dict[str, BookQuote] = {}
        self._lock = threading.Lock()
        self._desired: tuple[Instrument, ...] = ()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.counters: Counter[str] = Counter()
        self._errors: dict[str, str] = {}
        self._error_lock = threading.Lock()
        self._binance_stream = BinanceBookTickerStream(
            self._stop,
            self.counters,
            self._publish,
            self._set_error,
            self._clear_error,
            self._observe_quote,
            self._observe_depth,
            self._observe_reset,
        )
        self._hyperliquid_stream = HyperliquidBboStream(
            self._stop,
            self.counters,
            self._publish,
            self._set_error,
            self._clear_error,
            self._observe_quote,
            self._observe_depth,
            self._observe_reset,
        )

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._thread_main, name="public-market-ws", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def set_universe(self, instruments: tuple[Instrument, ...]) -> None:
        if instruments != self._desired:
            self._desired = instruments
            self._wake.set()

    def configure(
        self, symbols_per_connection: int, hl_subscriptions_per_connection: int, coalesce_ms: int
    ) -> None:
        changed = (
            symbols_per_connection != self.symbols_per_connection
            or hl_subscriptions_per_connection != self.hl_subscriptions_per_connection
            or coalesce_ms / 1000 != self.coalesce_seconds
        )
        self.symbols_per_connection = symbols_per_connection
        self.hl_subscriptions_per_connection = hl_subscriptions_per_connection
        self.coalesce_seconds = coalesce_ms / 1000
        if changed:
            self._wake.set()

    def snapshot(self) -> dict[str, BookQuote]:
        with self._lock:
            return dict(self._quotes)

    def configure_depth(self, enabled: bool, keys: frozenset[str] = frozenset()) -> None:
        if enabled != self.depth_enabled or keys != self.depth_keys:
            self.depth_enabled = enabled
            self.depth_keys = keys
            self._wake.set()

    def _observe_quote(self, instrument, bid, ask, bid_qty, ask_qty, received_ns, source_ms):
        if (
            self.event_sink
            and all(isfinite(v) for v in (bid, ask, bid_qty, ask_qty))
            and (0 < bid <= ask and min(bid_qty, ask_qty) >= 0)
        ):
            self.event_sink(
                QuoteEvent(instrument, bid, ask, bid_qty, ask_qty, received_ns, source_ms)
            )

    def _observe_depth(self, instrument, bids, asks, received_ns, source_ms):
        if not self.event_sink or not self.depth_enabled:
            return
        bids = tuple(sorted(((float(p), float(q)) for p, q in bids if float(q) > 0), reverse=True))
        asks = tuple(sorted((float(p), float(q)) for p, q in asks if float(q) > 0))
        if (
            not bids
            or not asks
            or bids[0][0] > asks[0][0]
            or any(not isfinite(v) or v <= 0 for level in (*bids, *asks) for v in level)
        ):
            self.counters["simulation_invalid_depth"] += 1
            return
        self.event_sink(DepthEvent(instrument, bids, asks, received_ns, source_ms))
        self.counters["simulation_depth_updates"] += 1

    def _observe_reset(self, instruments):
        if self.event_sink:
            self.event_sink(StreamReset(tuple(i.key for i in instruments), time.monotonic_ns()))

    def _thread_main(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        tasks: list[asyncio.Task[Any]] = []
        while not self._stop.is_set():
            changed = await asyncio.to_thread(self._wake.wait, 1.0)
            if self._stop.is_set():
                break
            # A timeout is just a heartbeat. Keep healthy sockets alive; only
            # rebuild the subscription tasks after the desired universe changes.
            if not changed:
                continue
            self._wake.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            rows = self._desired
            tasks = [
                asyncio.create_task(
                    self._binance_stream.run(
                        kind,
                        tuple(i for i in rows if i.venue == "binance" and i.kind == kind),
                        self.symbols_per_connection,
                        self.depth_enabled,
                        self.depth_keys,
                    )
                )
                for kind in ("spot", "perp")
            ]
            tasks.append(
                asyncio.create_task(
                    self._hyperliquid_stream.run(
                        tuple(i for i in rows if i.venue == "hyperliquid"),
                        self.hl_subscriptions_per_connection,
                        self.depth_enabled,
                        self.depth_keys,
                    )
                )
            )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _publish(
        self, instrument: Instrument, bid: float, ask: float, event_ms: int | None
    ) -> bool:
        if bid <= 0 or ask <= 0 or bid > ask:
            self.counters["invalid_quote"] += 1
            return False
        now_mono, now_wall = time.monotonic(), time.time()
        if event_ms:
            lag_ms = (now_wall * 1000) - event_ms
            # Diagnostic only: exchange and local clocks may have a fixed offset.
            previous = self.counters.get("event_lag_ewma_ms")
            self.counters["event_lag_ewma_ms"] = int(
                lag_ms if previous is None else previous * 0.9 + lag_ms * 0.1
            )
            self.counters["event_lag_max_ms"] = max(
                self.counters.get("event_lag_max_ms", 0), int(lag_ms)
            )
            if lag_ms > 1000:
                self.counters["event_lag_over_1s"] += 1
        with self._lock:
            self._quotes[instrument.key] = BookQuote(bid, ask, now_mono, now_wall, event_ms)
        self.counters["updates"] += 1
        return True

    @property
    def last_error(self) -> str:
        with self._error_lock:
            return " | ".join(f"{source}: {message}" for source, message in self._errors.items())

    def _set_error(self, source: str, message: str) -> None:
        with self._error_lock:
            self._errors[source] = message

    def _clear_error(self, source: str) -> None:
        with self._error_lock:
            self._errors.pop(source, None)
