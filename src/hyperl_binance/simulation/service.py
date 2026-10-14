from __future__ import annotations

import threading
import time
from queue import Empty, Full, Queue
from typing import Callable

from ..domain.market_events import MarketEvent
from .config import SimulationConfig
from .engine import SimulationEngine


class SimulationService:
    """A bounded independent consumer; slow simulation never blocks WS reception."""

    def __init__(self, config: SimulationConfig, emit: Callable[[dict], None]) -> None:
        self.engine = SimulationEngine(config, emit)
        self.queue: Queue = Queue(maxsize=20000)
        self._stop = threading.Event()
        self._overflow = threading.Event()
        self._lock = threading.Lock()
        self._view: dict[str, dict] = {}
        self._configuration = None
        self.queue_dropped = 0
        self.last_error = ""
        self._thread = threading.Thread(target=self._run, name="execution-simulation", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def ingest(self, event: MarketEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except Full:
            self.queue_dropped += 1
            self._overflow.set()

    def configure(self, *args) -> None:
        # Keep the latest control snapshot separately, so a full market-event
        # queue cannot block the CLI/statistics loop or lose a config change.
        with self._lock:
            self._configuration = args

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._view)

    def _run(self) -> None:
        last_publish, accept_after = 0.0, 0
        while not self._stop.is_set():
            try:
                with self._lock:
                    configuration, self._configuration = self._configuration, None
                if configuration is not None:
                    self.engine.configure(*configuration)
                if self._overflow.is_set():
                    self._overflow.clear()
                    accept_after = time.monotonic_ns()
                    self.engine.reset("simulation_queue_overflow")
                try:
                    item = self.queue.get(timeout=0.005)
                except Empty:
                    item = None
                if item is not None and item.received_ns >= accept_after:
                    self.engine.on_event(item)
                # Do not evaluate deadlines while older received events remain
                # queued. Their timestamps, not processing time, determine state.
                if self.queue.empty():
                    self.engine.advance(
                        time.monotonic_ns() - int(self.engine.config.resolution_grace_ms * 1e6)
                    )
                now = time.monotonic()
                if now - last_publish >= 0.2:
                    view = self.engine.view()
                    with self._lock:
                        self._view = view
                    last_publish = now
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.engine.reset("simulation_worker_error")
