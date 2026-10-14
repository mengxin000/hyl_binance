from __future__ import annotations

from collections import deque
from math import isclose

from ..domain.market_events import DepthEvent, QuoteEvent, StreamReset
from .config import SimulationConfig


class ObservationUnavailable(ValueError):
    pass


class OrderBookModel:
    """Bounded received-time histories of BBO and complete top-N snapshots."""

    def __init__(self, config: SimulationConfig) -> None:
        self.config = config
        self.quotes: dict[str, deque[QuoteEvent | StreamReset]] = {}
        self.books: dict[str, deque[DepthEvent | StreamReset]] = {}

    def add(self, event: QuoteEvent | DepthEvent | StreamReset) -> None:
        if isinstance(event, StreamReset):
            for key in event.keys:
                self._append(self.quotes, key, event)
                self._append(self.books, key, event)
        elif isinstance(event, QuoteEvent):
            self._append(self.quotes, event.instrument.key, event)
        else:
            self._append(self.books, event.instrument.key, event)

    def _append(self, histories: dict, key: str, event) -> None:
        history = histories.setdefault(key, deque(maxlen=self.config.history_events_per_instrument))
        if history and event.received_ns < history[-1].received_ns:
            return
        history.append(event)
        cutoff = event.received_ns - int(self.config.history_seconds * 1e9)
        while len(history) > 1 and history[1].received_ns < cutoff:
            history.popleft()

    def _at(self, histories: dict, key: str, target_ns: int):
        for event in reversed(histories.get(key, ())):
            if event.received_ns > target_ns:
                continue
            if isinstance(event, StreamReset):
                raise ObservationUnavailable("stream_reset")
            if target_ns - event.received_ns > self.config.max_data_age_ms * 1e6:
                raise ObservationUnavailable("stale_market_data")
            return event
        raise ObservationUnavailable("missing_market_data")

    def quote_at(self, key: str, target_ns: int) -> QuoteEvent:
        return self._at(self.quotes, key, target_ns)

    def sweep(self, key: str, side: str, quantity: float, target_ns: int) -> tuple[float, str]:
        quote = self.quote_at(key, target_ns)
        top, size = (quote.ask, quote.ask_qty) if side == "buy" else (quote.bid, quote.bid_qty)
        if quantity <= size:
            return top, "bbo"
        book = self._at(self.books, key, target_ns)
        levels = book.asks if side == "buy" else book.bids
        if (
            not levels
            or not isclose(levels[0][0], top, rel_tol=1e-9)
            or not isclose(levels[0][1], size, rel_tol=1e-6, abs_tol=1e-9)
        ):
            raise ObservationUnavailable("book_bbo_mismatch")
        remaining, total = quantity, 0.0
        for price, available in levels:
            take = min(remaining, available)
            total += take * price
            remaining -= take
            if remaining <= quantity * 1e-10:
                return total / quantity, "depth"
        raise ObservationUnavailable("insufficient_observed_depth")
