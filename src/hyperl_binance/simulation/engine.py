from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Callable

from ..config.settings import MarketConfig
from ..domain.market_events import MarketEvent, QuoteEvent, StreamReset
from ..domain.models import Instrument, SpreadRoute
from ..domain.thresholds import TriggerLevels
from .config import SimulationConfig
from .expectation import ExpectationModel
from .fees import FeeModel
from .latency import LatencyModel
from .orderbook import ObservationUnavailable, OrderBookModel


@dataclass(frozen=True, slots=True)
class SignalPolicy:
    levels: TriggerLevels
    available_ns: int = 0


def execution_legs(route: SpreadRoute) -> tuple[Instrument, Instrument]:
    maker = (
        (route.hyperliquid if route.hyperliquid.kind == "spot" else route.binance)
        if route.category == "spot_perp"
        else route.binance
    )
    taker = route.binance if maker is route.hyperliquid else route.hyperliquid
    return maker, taker


@dataclass(slots=True)
class Observation:
    route: SpreadRoute
    phase: str
    maker: Instrument
    taker: Instrument
    maker_side: str
    signal_ns: int
    maker_target_ns: int
    taker_target_ns: int
    maker_reference: float
    taker_reference: float
    config: SimulationConfig
    trigger_levels: TriggerLevels | None = None
    signal_basis_bps: float = 0.0
    mean_bps: float = 0.0
    maker_quote_ns: int | None = None
    maker_price: float | None = None
    quantity: float | None = None


class SimulationEngine:
    """Pure event-time engine; no dependency on RouteHistory or CLI sampling."""

    def __init__(self, config: SimulationConfig, emit: Callable[[dict], None] | None = None):
        self.config = config
        self.scenario_id = self._scenario_id(config)
        self.emit = emit or (lambda _record: None)
        self.books = OrderBookModel(config)
        self.routes: dict[str, SpreadRoute] = {}
        self.by_instrument: dict[str, list[str]] = {}
        self.converters: tuple[Instrument, ...] = ()
        self.policies: dict[str, SignalPolicy] = {}
        self.market = MarketConfig()
        self.pending: dict[str, Observation] = {}
        self.gates: dict[str, tuple[bool, bool]] = {}
        self.results: dict[str, ExpectationModel] = {}

    @staticmethod
    def _scenario_id(config: SimulationConfig) -> str:
        return hashlib.sha256(json.dumps(asdict(config), sort_keys=True).encode()).hexdigest()[:12]

    def configure(
        self,
        routes: tuple[SpreadRoute, ...],
        instruments: tuple[Instrument, ...],
        policies: dict[str, SignalPolicy],
        config: SimulationConfig,
        market: MarketConfig,
    ) -> None:
        # A different amount/delay/fee scenario starts a new comparable sample set.
        if config != self.config:
            self.pending.clear()
            self.gates.clear()
            self.results.clear()
            self.policies.clear()
            self.books = OrderBookModel(config)
            self.config = config
            self.scenario_id = self._scenario_id(config)
            self.emit(
                {
                    "type": "scenario_reset",
                    "reason": "simulation_config_changed",
                    "scenario_id": self.scenario_id,
                    "config": asdict(config),
                }
            )
        self.routes = {route.route_id: route for route in routes}
        for rid, policy in policies.items():
            previous = self.policies.get(rid)
            if previous is not None and previous.levels.rule_id != policy.levels.rule_id:
                if rid in self.pending:
                    self._fail(rid, "threshold_rule_changed")
                self.results.pop(rid, None)
                self.gates.pop(rid, None)
                self.emit(
                    {
                        "type": "threshold_rule_reset",
                        "route": rid,
                        "old_rule_id": previous.levels.rule_id,
                        "trigger": asdict(policy.levels),
                        "scenario_id": self.scenario_id,
                    }
                )
        self.policies, self.market = policies, market
        self.by_instrument = {}
        for route in routes:
            for leg in (route.hyperliquid, route.binance):
                self.by_instrument.setdefault(leg.key, []).append(route.route_id)
        self.converters = tuple(
            i
            for i in instruments
            if i.venue == "binance"
            and i.kind == "spot"
            and (i.base, i.quote) in (("USDC", "USDT"), ("USDT", "USDC"))
        )
        for rid in tuple(self.pending):
            if rid not in self.routes:
                self._fail(rid, "route_removed")
        for rid in tuple(self.results):
            if rid not in self.routes:
                self.results.pop(rid)
                self.gates.pop(rid, None)
        # Include waiting routes in snapshots so the dashboard can display
        # warm-up progress and the actual levels even before any execution.
        for rid in self.routes:
            if rid not in self.results:
                self.results[rid] = ExpectationModel(self.config.window_samples)

    def reset(self, reason: str) -> None:
        for rid in tuple(self.pending):
            self._fail(rid, reason)
        self.books = OrderBookModel(self.config)
        self.gates.clear()

    def on_event(self, event: MarketEvent) -> None:
        # Finish earlier deadlines BEFORE adding a later event, even if a worker
        # wakes late or processes a batch. An event at the deadline is allowed.
        self.advance(event.received_ns - 1)
        self.books.add(event)
        if isinstance(event, StreamReset):
            keys = set(event.keys)
            for rid, job in tuple(self.pending.items()):
                if {job.maker.key, job.taker.key}.intersection(keys) or any(
                    converter.key in keys for converter in self.converters
                ):
                    self._fail(rid, "stream_reset")
            for key in keys:
                for rid in self.by_instrument.get(key, ()):
                    self.gates.pop(rid, None)
        elif isinstance(event, QuoteEvent) and self.config.enabled:
            for rid in self.by_instrument.get(event.instrument.key, ()):
                self._signal(rid, event.received_ns)
        self.advance(event.received_ns)

    def _rate(self, instrument: Instrument, target_ns: int) -> float:
        if instrument.quote == "USDT":
            return 1.0
        latest = None
        for converter in self.converters:
            try:
                quote = self.books.quote_at(converter.key, target_ns)
            except ObservationUnavailable:
                continue
            if latest is None or quote.received_ns > latest.received_ns:
                latest = quote
        if latest is None:
            raise ObservationUnavailable("missing_usdc_conversion")
        mid = (latest.bid + latest.ask) / 2
        return mid if latest.instrument.base == "USDC" else 1 / mid

    def _signal(self, rid: str, t0: int) -> None:
        policy, route = self.policies.get(rid), self.routes[rid]
        if policy is None or not policy.levels.ready or t0 < policy.available_ns:
            return
        levels = policy.levels
        assert levels.open_bps is not None and levels.close_bps is not None
        try:
            h = self.books.quote_at(route.hyperliquid.key, t0)
            b = self.books.quote_at(route.binance.key, t0)
            hr, br = self._rate(route.hyperliquid, t0), self._rate(route.binance, t0)
        except ObservationUnavailable:
            return
        hm, bm = (h.bid + h.ask) / 2 * hr, (b.bid + b.ask) / 2 * br
        if route.requires_base_price_validation and abs(hm / bm - 1) > 0.01:
            return
        if self.market.binance_price_min_usdt is not None and (
            bm < self.market.binance_price_min_usdt
        ):
            return
        if route.category == "spot_perp":
            maker, taker = execution_legs(route)
            mq, tq, mr, tr = (h, b, hr, br) if maker is route.hyperliquid else (b, h, br, hr)
            value = (tq.ask * tr / (mq.bid * mr) - 1) * 10000
        else:
            maker, taker, mq, tq, mr, tr = route.binance, route.hyperliquid, b, h, br, hr
            value = (hm / bm - 1) * 10000
        above, below = self.gates.get(rid, (False, False))
        phase = None
        if value > levels.open_bps:
            if not above:
                phase = "open"
            above = True
        elif value <= levels.rearm_bps:
            above = False
        if value < levels.close_bps:
            if not below:
                phase = "close"
            below = True
        elif value >= levels.rearm_bps:
            below = False
        self.gates[rid] = above, below
        if phase is None or rid in self.pending:
            return
        targets = LatencyModel.targets(t0, self.config)
        self.pending[rid] = Observation(
            route,
            phase,
            maker,
            taker,
            "buy" if phase == "open" else "sell",
            t0,
            targets.maker_ns,
            targets.taker_ns,
            (mq.bid if phase == "open" else mq.ask) * mr,
            (tq.bid if phase == "open" else tq.ask) * tr,
            self.config,
            trigger_levels=levels,
            signal_basis_bps=value,
            mean_bps=levels.mean_bps,
        )
        self.results.setdefault(rid, ExpectationModel(self.config.window_samples)).attempts += 1

    def advance(self, now_ns: int) -> None:
        for rid, job in tuple(self.pending.items()):
            try:
                if job.maker_price is None and now_ns >= job.maker_target_ns:
                    q = self.books.quote_at(job.maker.key, job.maker_target_ns)
                    price = q.bid if job.maker_side == "buy" else q.ask
                    job.maker_price = price * self._rate(job.maker, job.maker_target_ns)
                    job.maker_quote_ns = q.received_ns
                    job.quantity = job.config.order_notional_usdt / job.maker_price
                if now_ns >= job.taker_target_ns:
                    self._finish(rid, job)
            except ObservationUnavailable as exc:
                self._fail(rid, str(exc))

    def _finish(self, rid: str, job: Observation) -> None:
        assert job.quantity is not None and job.maker_price is not None
        side = "sell" if job.maker_side == "buy" else "buy"
        top = self.books.quote_at(job.taker.key, job.taker_target_ns)
        rate = self._rate(job.taker, job.taker_target_ns)
        raw_vwap, depth_source = self.books.sweep(
            job.taker.key, side, job.quantity, job.taker_target_ns
        )
        taker_top = (top.bid if side == "sell" else top.ask) * rate
        vwap = raw_vwap * rate
        sign = 1 if job.maker_side == "buy" else -1
        scale = job.quantity / job.config.order_notional_usdt * 10000
        delay = (
            sign * (job.maker_price - job.maker_reference + job.taker_reference - taker_top) * scale
        )
        depth = sign * (taker_top - vwap) * scale
        maker_fee = FeeModel.charge(job.config, job.maker, "maker", job.quantity, job.maker_price)
        taker_fee = FeeModel.charge(job.config, job.taker, "taker", job.quantity, vwap)
        fees = maker_fee + taker_fee
        fee_bps = fees / job.config.order_notional_usdt * 10000
        sample = {
            "type": "execution_observation",
            "scenario_id": self.scenario_id,
            "route": rid,
            "phase": job.phase,
            "signal_basis_bps": job.signal_basis_bps,
            "mean_bps": job.mean_bps,
            "trigger": asdict(job.trigger_levels) if job.trigger_levels is not None else None,
            "signal_ns": job.signal_ns,
            "maker_target_ns": job.maker_target_ns,
            "taker_target_ns": job.taker_target_ns,
            "quantity": job.quantity,
            "order_notional_usdt": job.config.order_notional_usdt,
            "exchange_delay_ms": job.config.exchange_delay_ms,
            "deal_delay_ms": job.config.deal_delay_ms,
            "maker": job.maker.key,
            "taker": job.taker.key,
            "maker_side": job.maker_side,
            "maker_price_usdt": job.maker_price,
            "maker_reference_usdt": job.maker_reference,
            "taker_reference_usdt": job.taker_reference,
            "maker_quote_ns": job.maker_quote_ns,
            "taker_quote_ns": top.received_ns,
            "maker_fee_usdt": maker_fee,
            "taker_fee_usdt": taker_fee,
            "taker_vwap_usdt": vwap,
            "delay_bps": delay,
            "depth_bps": depth,
            "total_slippage_bps": delay + depth,
            "fee_bps": fee_bps,
            "cost_bps": delay + depth + fee_bps,
            "net_edge_bps": sign * (vwap - job.maker_price) * scale - fee_bps,
            "depth_source": depth_source,
            "maker_fill_assumed": True,
        }
        self.results[rid].record(sample)
        self.emit(sample)
        self.pending.pop(rid)

    def _fail(self, rid: str, reason: str) -> None:
        job = self.pending.pop(rid)
        self.results[rid].record_unavailable(job.phase, reason)
        self.emit(
            {
                "type": "unavailable_observation",
                "scenario_id": self.scenario_id,
                "route": rid,
                "phase": job.phase,
                "signal_ns": job.signal_ns,
                "trigger": asdict(job.trigger_levels) if job.trigger_levels is not None else None,
                "reason": reason,
            }
        )

    def view(self) -> dict[str, dict]:
        return {
            rid: {
                **result.view(),
                "pending": rid in self.pending,
                "trigger": asdict(self.policies[rid].levels) if rid in self.policies else None,
            }
            for rid, result in self.results.items()
        }
