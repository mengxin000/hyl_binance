from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from typing import Any

from ..domain.models import BookQuote, Instrument
from .binance_stream import BinanceBookTickerStream
from .hyperliquid_stream import HyperliquidBboStream


class PublicFeed:
    """Independent async WS thread; publish only latest quote per instrument."""

    def __init__(
        self, symbols_per_connection: int, hl_subscriptions_per_connection: int, coalesce_ms: int
    ) -> None:
        self.symbols_per_connection = symbols_per_connection
        self.hl_subscriptions_per_connection = hl_subscriptions_per_connection
        self.coalesce_seconds = coalesce_ms / 1000
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
            self._stop, self.counters, self._publish, self._set_error, self._clear_error
        )
        self._hyperliquid_stream = HyperliquidBboStream(
            self._stop, self.counters, self._publish, self._set_error, self._clear_error
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
                    )
                )
                for kind in ("spot", "perp")
            ]
            tasks.append(
                asyncio.create_task(
                    self._hyperliquid_stream.run(
                        tuple(i for i in rows if i.venue == "hyperliquid"),
                        self.hl_subscriptions_per_connection,
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
