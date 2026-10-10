from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from typing import Callable
from urllib.parse import quote

import websockets

from ..domain.models import Instrument

BINANCE_WS = {
    "spot": "wss://stream.binance.com:9443/stream?streams=",
    "perp": "wss://fstream.binance.com/stream?streams=",
}


class BinanceBookTickerStream:
    """Binance public bookTicker connections, independent from feed orchestration."""

    def __init__(
        self,
        stop_event: threading.Event,
        counters: Counter[str],
        publish: Callable[[Instrument, float, float, int | None], bool],
        set_error: Callable[[str, str], None],
        clear_error: Callable[[str], None],
    ) -> None:
        self.stop_event = stop_event
        self.counters = counters
        self.publish = publish
        self.set_error = set_error
        self.clear_error = clear_error

    async def run(
        self, kind: str, instruments: tuple[Instrument, ...], symbols_per_connection: int
    ) -> None:
        self.symbols_per_connection = symbols_per_connection
        await self._binance_loop(kind, instruments)

    async def _binance_loop(self, kind: str, instruments: tuple[Instrument, ...]) -> None:
        by_symbol = {i.feed_coin.lower(): i for i in instruments}
        if not by_symbol:
            return
        names = sorted(by_symbol)
        streams = [f"{name}@bookTicker" for name in names]
        shards = [
            streams[n : n + self.symbols_per_connection]
            for n in range(0, len(streams), self.symbols_per_connection)
        ]
        await asyncio.gather(
            *(
                self._binance_shard(kind, index, shard, by_symbol)
                for index, shard in enumerate(shards)
            )
        )

    async def _binance_shard(
        self, kind: str, shard_index: int, streams: list[str], by_symbol: dict[str, Instrument]
    ) -> None:
        error_key = f"Binance {kind} shard {shard_index + 1}"
        url = BINANCE_WS[kind] + "/".join(quote(s, safe="@") for s in streams)
        delay = 1.0
        while not self.stop_event.is_set():
            counted = False
            connected_at: float | None = None
            error_message = ""
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=3,
                    max_queue=1,
                    compression=None,
                ) as ws:
                    connected_at = asyncio.get_running_loop().time()
                    self.clear_error(error_key)
                    self.counters["connection_opens"] += 1
                    self.counters["active_connections"] += 1
                    self.counters[f"binance_{kind}_connection_opens"] += 1
                    self.counters[f"binance_{kind}_active_connections"] += 1
                    counted = True
                    async for raw in ws:
                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        inst = by_symbol.get(str(data.get("s", "")).lower())
                        if inst:
                            try:
                                self.publish(
                                    inst,
                                    float(data["b"]),
                                    float(data["a"]),
                                    int(data.get("E", 0)) or None,
                                )
                            except (KeyError, TypeError, ValueError):
                                self.counters["parse_errors"] += 1
                    if not self.stop_event.is_set():
                        error_message = "connection closed; reconnecting"
            except asyncio.CancelledError:
                self.clear_error(error_key)
                raise
            except Exception as exc:
                error_message = f"{type(exc).__name__}: {exc}"
            finally:
                if counted:
                    self.counters["active_connections"] -= 1
                    self.counters[f"binance_{kind}_active_connections"] -= 1

            if self.stop_event.is_set():
                break
            loop = asyncio.get_running_loop()
            if connected_at is not None and loop.time() - connected_at >= 60:
                delay = 1.0
            self.set_error(error_key, error_message or "reconnecting")
            self.counters["reconnects"] += 1
            self.counters[f"binance_{kind}_reconnects"] += 1
            await asyncio.to_thread(self.stop_event.wait, delay)
            delay = min(delay * 2, 30)
