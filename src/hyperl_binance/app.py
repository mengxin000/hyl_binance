from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .config.settings import CONFIG_DIR, ConfigManager, RuntimeConfig, load_config_managers
from .domain.screener import (
    binance_price_filter_reason,
    build_routes,
    calculate_snapshot,
    conversion_rates,
)
from .domain.statistics import RouteHistory
from .exchanges.discovery import discover_markets
from .market_data.feed import PublicFeed
from .paths import project_path
from .persistence.storage import Storage
from .presentation.terminal import PAGES, render
from .simulation.config import ScoreConfig, SimulationConfig
from .simulation.engine import SignalPolicy, execution_legs
from .simulation.service import SimulationService

FUNDING_HISTORY_SECONDS = 30 * 24 * 60 * 60
FUNDING_SAMPLE_INTERVAL_SECONDS = 60 * 60


def _load_funding_history(
    root: Path, now: float
) -> dict[str, list[tuple[float, float | None, float | None]]]:
    directory = root / "funding_history"
    if not directory.exists():
        return {}
    cutoff = now - FUNDING_HISTORY_SECONDS
    result: dict[str, list[tuple[float, float | None, float | None]]] = {}
    for path in sorted(directory.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                        sampled_at = float(item["ts"])
                        route_id = str(item["route"])
                        if not cutoff <= sampled_at <= now:
                            continue
                        hl = item.get("funding_hl")
                        binance = item.get("funding_binance")
                        result.setdefault(route_id, []).append(
                            (
                                sampled_at,
                                float(hl) if hl is not None else None,
                                float(binance) if binance is not None else None,
                            )
                        )
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
        except OSError:
            continue
    for samples in result.values():
        samples.sort(key=lambda sample: sample[0])
    return result


def _prune_funding_history(root: Path, now: float) -> None:
    directory = root / "funding_history"
    if not directory.exists():
        return
    cutoff_day = time.strftime("%Y-%m-%d", time.gmtime(now - FUNDING_HISTORY_SECONDS))
    for path in directory.glob("*.jsonl"):
        if path.stem < cutoff_day:
            try:
                path.unlink()
            except OSError:
                pass


async def run() -> None:
    market_manager, spot_spot_manager, spot_perp_manager, perp_perp_manager, runtime_manager = (
        load_config_managers()
    )
    stat_managers = {
        "spot_spot": spot_spot_manager,
        "spot_perp": spot_perp_manager,
        "perp_perp": perp_perp_manager,
    }
    runtime: RuntimeConfig = runtime_manager.current
    simulation_manager = ConfigManager(CONFIG_DIR / "simulation.json", SimulationConfig)
    score_manager = ConfigManager(CONFIG_DIR / "score.json", ScoreConfig)
    storage = Storage(project_path(runtime.data_directory), runtime.network_log_max_bytes)
    storage.start()
    simulation = SimulationService(
        simulation_manager.current,
        lambda record: storage.emit("simulation/observations.jsonl", {"ts": time.time(), **record}),
    )
    simulation.start()
    funding_history = _load_funding_history(storage.root, time.time())
    _prune_funding_history(storage.root, time.time())
    feed = PublicFeed(
        runtime.binance_symbols_per_connection,
        runtime.hyperliquid_subscriptions_per_connection,
        runtime.raw_coalesce_interval_ms,
        event_sink=simulation.ingest,
    )
    feed.configure_depth(simulation_manager.current.enabled)
    feed.start()
    histories: dict[str, RouteHistory] = {}
    errors: dict[str, str] = {}
    instruments = ()
    routes = ()
    discovered_counts: dict[str, int] = {}
    qualified_counts: dict[str, int] = {}
    direction_counts: dict[str, int] = {}
    retained_categories: set[tuple[str, str]] = set()
    page_index = 0
    last_discovery = last_render = last_persist = last_diag = 0.0
    last_funding_sample = last_funding_cleanup = 0.0
    try:
        while True:
            loop_time = time.monotonic()
            old_market_config = market_manager.current
            market_changed = market_manager.reload()
            for manager in (
                *stat_managers.values(),
                runtime_manager,
                simulation_manager,
                score_manager,
            ):
                manager.reload()
            feed.configure_depth(
                simulation_manager.current.enabled,
                frozenset(execution_legs(route)[1].key for route in routes),
            )
            discovery_fields = (
                "quote_assets",
                "hyperliquid_min_spot_volume_usdt",
                "hyperliquid_min_perp_volume_usdt",
                "binance_min_spot_volume_usdt",
                "binance_min_perp_volume_usdt",
                "base_aliases",
                "hyperliquid_perp_quote_overrides",
            )
            if market_changed and any(
                getattr(old_market_config, field) != getattr(market_manager.current, field)
                for field in discovery_fields
            ):
                last_discovery = 0.0
            new_runtime = runtime_manager.current
            if new_runtime != runtime:
                runtime = new_runtime
                feed.configure(
                    runtime.binance_symbols_per_connection,
                    runtime.hyperliquid_subscriptions_per_connection,
                    runtime.raw_coalesce_interval_ms,
                )
            if loop_time - last_discovery >= runtime.market_refresh_seconds or not instruments:
                result = await asyncio.to_thread(discover_markets, market_manager.current)
                discovered_instruments = list(result.instruments)
                retained_categories.clear()
                failed_categories = {
                    "hyperliquid_spot_metadata": ("hyperliquid", "spot"),
                    "hyperliquid_perp_metadata": ("hyperliquid", "perp"),
                    "binance_spot_metadata": ("binance", "spot"),
                    "binance_perp_metadata": ("binance", "perp"),
                }
                # A transient REST/TLS failure must not erase a previously
                # discovered market category and its working subscriptions.
                for error_key, category_key in failed_categories.items():
                    cached = [
                        item for item in instruments if (item.venue, item.kind) == category_key
                    ]
                    if error_key in result.errors and cached:
                        discovered_instruments = [
                            item
                            for item in discovered_instruments
                            if (item.venue, item.kind) != category_key
                        ]
                        discovered_instruments.extend(cached)
                        retained_categories.add(category_key)
                instruments = tuple(discovered_instruments)
                errors = {
                    **result.errors,
                    **{
                        m.path.name: m.error
                        for m in (market_manager, runtime_manager, *stat_managers.values())
                        if m.error
                    },
                }
                routes = build_routes(instruments, market_manager.current)
                feed.configure_depth(
                    simulation_manager.current.enabled,
                    frozenset(execution_legs(route)[1].key for route in routes),
                )
                category_counts = {
                    category: sum(route.category == category for route in routes)
                    for category in ("spot_spot", "spot_perp", "perp_perp")
                }
                discovered = Counter(f"{item.venue}_{item.kind}" for item in instruments)
                qualified = Counter()
                for item in instruments:
                    if item.base in market_manager.current.quote_assets:
                        continue
                    minimum = getattr(
                        market_manager.current, f"{item.venue}_min_{item.kind}_volume_usdt"
                    )
                    if item.quote_volume >= minimum:
                        qualified[f"{item.venue}_{item.kind}"] += 1
                discovered_counts = dict(discovered)
                qualified_counts = dict(qualified)
                direction_counts = dict(
                    Counter(
                        f"h_{route.hyperliquid.kind}_b_{route.binance.kind}" for route in routes
                    )
                )
                aliases = market_manager.current.base_aliases or {}

                def canonical_base(item) -> str:
                    return aliases.get(
                        f"{item.venue}:{item.base}", aliases.get(item.base, item.base)
                    ).upper()

                eligible = {
                    (venue, kind): [
                        item
                        for item in instruments
                        if item.venue == venue
                        and item.kind == kind
                        and item.base not in market_manager.current.quote_assets
                        and item.quote_volume
                        >= getattr(market_manager.current, f"{venue}_min_{kind}_volume_usdt")
                    ]
                    for venue, kind in (
                        ("binance", "spot"),
                        ("binance", "perp"),
                        ("hyperliquid", "spot"),
                        ("hyperliquid", "perp"),
                    )
                }
                hl_spot_routes = {
                    item.key: [route for route in routes if route.hyperliquid.key == item.key]
                    for item in eligible[("hyperliquid", "spot")]
                }
                hl_perp_eligible_keys = {item.key for item in eligible[("hyperliquid", "perp")]}
                hl_perp_routes = {
                    item.key: [route for route in routes if route.hyperliquid.key == item.key]
                    for item in eligible[("hyperliquid", "perp")]
                }
                hl_perp_threshold = market_manager.current.hyperliquid_min_perp_volume_usdt
                storage.save_state(
                    "diagnostics/market_universe.json",
                    {
                        "ts": time.time(),
                        "hyperliquid_perp_scope": "default perp DEX only; HIP-3 excluded",
                        "hyperliquid_perp_min_volume_usdt": hl_perp_threshold,
                        "hyperliquid_perp_discovered_count": len(
                            [
                                item
                                for item in instruments
                                if item.venue == "hyperliquid" and item.kind == "perp"
                            ]
                        ),
                        "hyperliquid_perp_qualified_count": len(hl_perp_eligible_keys),
                        "hyperliquid_perp_markets": [
                            {
                                "symbol": item.symbol,
                                "base": item.base,
                                "quote": item.quote,
                                "day_ntl_volume": item.quote_volume,
                                "volume_qualified": item.key in hl_perp_eligible_keys,
                                "matched_eligible_binance_symbols": sorted(
                                    {
                                        route.binance.symbol
                                        for route in hl_perp_routes.get(item.key, [])
                                    }
                                ),
                            }
                            for item in instruments
                            if item.venue == "hyperliquid" and item.kind == "perp"
                        ],
                        "hyperliquid_spot_eligible": [
                            {
                                "symbol": item.symbol,
                                "base": item.base,
                                "canonical_base": canonical_base(item),
                                "quote": item.quote,
                                "quote_volume_24h": item.quote_volume,
                                "matches_binance_spot": any(
                                    route.binance.kind == "spot"
                                    for route in hl_spot_routes[item.key]
                                ),
                                "matches_binance_perp": any(
                                    route.binance.kind == "perp"
                                    for route in hl_spot_routes[item.key]
                                ),
                                "binance_candidate_symbols": sorted(
                                    {route.binance.symbol for route in hl_spot_routes[item.key]}
                                ),
                                "price_validation_required": any(
                                    route.requires_base_price_validation
                                    for route in hl_spot_routes[item.key]
                                ),
                            }
                            for item in eligible[("hyperliquid", "spot")]
                        ],
                    },
                )
                # Subscribe only route legs plus each venue's USDC/USDT converter.
                needed = {leg.key for route in routes for leg in (route.hyperliquid, route.binance)}
                needed.update(
                    i.key
                    for i in instruments
                    if i.venue in {"hyperliquid", "binance"}
                    and i.kind == "spot"
                    and (i.base, i.quote) in (("USDC", "USDT"), ("USDT", "USDC"))
                )
                feed.set_universe(tuple(i for i in instruments if i.key in needed))
                for route in routes:
                    cfg = stat_managers[route.category].current
                    history = histories.get(route.route_id)
                    if history is None:
                        history = histories[route.route_id] = RouteHistory(
                            cfg, runtime.quantile_refresh_seconds
                        )
                        history.load_funding_samples(
                            funding_history.get(route.route_id, []), time.time()
                        )
                    history.config = cfg
                    history.quantile_refresh_seconds = runtime.quantile_refresh_seconds
                metadata_retry_seconds = 30.0
                if any(key in result.errors for key in failed_categories):
                    last_discovery = loop_time - max(
                        runtime.market_refresh_seconds - metadata_retry_seconds, 0.0
                    )
                else:
                    last_discovery = loop_time
            elif errors:
                errors = {
                    **errors,
                    **{
                        m.path.name: m.error
                        for m in (market_manager, runtime_manager, *stat_managers.values())
                        if m.error
                    },
                }

            quotes = feed.snapshot()
            now = time.monotonic()
            rates = conversion_rates(instruments, quotes, now, runtime.quote_max_age_ms / 1000)
            valid_count = 0
            skew_over_tolerance = 0
            invalid: dict[str, int] = {}
            valid_by_direction: Counter[str] = Counter()
            invalid_by_direction: dict[str, Counter[str]] = {}
            valid_route_ids: set[str] = set()
            price_excluded_route_ids: set[str] = set()
            # Evaluate each route once per sample tick. Missing or asynchronous quotes
            # are counted by reason for diagnosis and never fed into statistics.
            for route in routes:
                snapshot, reason = calculate_snapshot(
                    route, quotes, rates, now, runtime.quote_max_age_ms / 1000
                )
                if snapshot:
                    price_reason = binance_price_filter_reason(snapshot, market_manager.current)
                    if price_reason:
                        snapshot, reason = None, price_reason
                        price_excluded_route_ids.add(route.route_id)
                if snapshot:
                    if snapshot.receive_skew_ms > runtime.quote_match_tolerance_ms:
                        skew_over_tolerance += 1
                    history = histories[route.route_id]
                    history.config = stat_managers[route.category].current
                    history.quantile_refresh_seconds = runtime.quantile_refresh_seconds
                    event = history.add(snapshot)
                    if event:
                        storage.emit(
                            f"{route.category}/{route.base}/opportunities.jsonl",
                            {
                                "ts": time.time(),
                                "route": route.route_id,
                                "spread_bps": snapshot.spread_bps,
                                "open_count": history.stats.open_count,
                                "close_count": history.stats.close_count,
                            },
                        )
                    valid_count += 1
                    valid_route_ids.add(route.route_id)
                    valid_by_direction[f"h_{route.hyperliquid.kind}_b_{route.binance.kind}"] += 1
                else:
                    invalid[reason] = invalid.get(reason, 0) + 1
                    direction = f"h_{route.hyperliquid.kind}_b_{route.binance.kind}"
                    invalid_by_direction.setdefault(direction, Counter())[reason] += 1
            policy_time = time.monotonic_ns()
            policies = {
                route.route_id: SignalPolicy(
                    histories[route.route_id].stats.mean_bps,
                    stat_managers[route.category].current.open_threshold_bps,
                    stat_managers[route.category].current.close_threshold_bps,
                    policy_time,
                )
                for route in routes
                if histories[route.route_id].stats.sample_count >= 2
            }
            simulation.configure(
                routes, instruments, policies, simulation_manager.current, market_manager.current
            )
            simulation_view = simulation.snapshot()
            wall_now = time.time()
            if wall_now - last_funding_sample >= FUNDING_SAMPLE_INTERVAL_SECONDS:
                day = time.strftime("%Y-%m-%d", time.gmtime(wall_now))
                for route in routes:
                    history = histories.get(route.route_id)
                    if history is None or (
                        route.hyperliquid.funding_rate is None
                        and route.binance.funding_rate is None
                    ):
                        continue
                    history.add_funding_sample(
                        wall_now, route.hyperliquid.funding_rate, route.binance.funding_rate
                    )
                    storage.emit(
                        f"funding_history/{day}.jsonl",
                        {
                            "ts": wall_now,
                            "route": route.route_id,
                            "funding_hl": route.hyperliquid.funding_rate,
                            "funding_binance": route.binance.funding_rate,
                        },
                    )
                last_funding_sample = wall_now
            if wall_now - last_funding_cleanup >= 24 * 60 * 60:
                _prune_funding_history(storage.root, wall_now)
                last_funding_cleanup = wall_now
            if loop_time - last_diag >= 5:
                quote_counts = Counter()
                for item in instruments:
                    quote = quotes.get(item.key)
                    if quote and 0 <= now - quote.received_mono <= runtime.quote_max_age_ms / 1000:
                        quote_counts[f"{item.venue}_{item.kind}"] += 1
                hl_shards = {
                    key[len("hl_shard_") : -len("_last_sent")]: {
                        "subscriptions_sent": value,
                        "subscription_acks": feed.counters.get(
                            key[: -len("_last_sent")] + "_last_acks", 0
                        ),
                        "last_uptime_ms": feed.counters.get(
                            key[: -len("_last_sent")] + "_last_uptime_ms", 0
                        ),
                    }
                    for key, value in feed.counters.items()
                    if key.startswith("hl_shard_") and key.endswith("_last_sent")
                }
                status = {
                    "ts": time.time(),
                    "active_connections": feed.counters.get("active_connections", 0),
                    "connection_opens_total": feed.counters.get("connection_opens", 0),
                    "reconnects": feed.counters.get("reconnects", 0),
                    "updates": feed.counters.get("updates", 0),
                    "parse_errors": feed.counters.get("parse_errors", 0),
                    "invalid_quotes": feed.counters.get("invalid_quote", 0),
                    "event_lag_ewma_ms_clock_offset_sensitive": feed.counters.get(
                        "event_lag_ewma_ms", 0
                    ),
                    "event_lag_max_ms_clock_offset_sensitive": feed.counters.get(
                        "event_lag_max_ms", 0
                    ),
                    "event_lag_over_1s": feed.counters.get("event_lag_over_1s", 0),
                    "valid_routes": valid_count,
                    "valid_routes_by_direction": dict(valid_by_direction),
                    "receive_skew_over_tolerance_routes": skew_over_tolerance,
                    "receive_skew_tolerance_ms_diagnostic_only": runtime.quote_match_tolerance_ms,
                    "invalid_routes": invalid,
                    "invalid_routes_by_direction": {
                        key: dict(value) for key, value in invalid_by_direction.items()
                    },
                    "discovered_instruments_by_venue_kind": discovered_counts,
                    "volume_eligible_instruments_by_venue_kind": qualified_counts,
                    "route_count": len(routes),
                    "route_directions": direction_counts,
                    "quote_count": len(quotes),
                    "quotes_by_venue_kind": dict(quote_counts),
                    "hl_bbo_subscription_acks": feed.counters.get("hl_bbo_subscription_acks", 0),
                    "hl_subscriptions_sent_total": feed.counters.get(
                        "hl_subscriptions_sent_total", 0
                    ),
                    "hl_last_connection_subscriptions_sent": sum(
                        item["subscriptions_sent"] for item in hl_shards.values()
                    ),
                    "hl_last_connection_subscription_acks": sum(
                        item["subscription_acks"] for item in hl_shards.values()
                    ),
                    "hl_shards_configured": feed.counters.get("hl_shards_configured", 0),
                    "hl_shard_subscription_status": hl_shards,
                    "hl_isolated_subscriptions_total": feed.counters.get(
                        "hl_isolated_subscriptions_total", 0
                    ),
                    "hl_subscription_ack_timeouts": feed.counters.get(
                        "hl_subscription_ack_timeouts", 0
                    ),
                    "hl_active_connections": feed.counters.get("hl_active_connections", 0),
                    "hl_connection_opens_total": feed.counters.get("hl_connection_opens", 0),
                    "hl_reconnects": feed.counters.get("hl_reconnects", 0),
                    "hl_pings_sent": feed.counters.get("hl_pings_sent", 0),
                    "hl_pongs_received": feed.counters.get("hl_pongs_received", 0),
                    "hl_ping_timeouts": feed.counters.get("hl_ping_timeouts", 0),
                    "hl_last_connection_uptime_ms": feed.counters.get(
                        "hl_last_connection_uptime_ms", 0
                    ),
                    "hl_subscription_errors": feed.counters.get("hl_subscription_errors", 0),
                    "hl_unmatched_bbo_messages": feed.counters.get("hl_unmatched_bbo_messages", 0),
                    "hl_bbo_messages_spot": feed.counters.get("hl_bbo_messages_spot", 0),
                    "hl_empty_bbo_messages_spot": feed.counters.get(
                        "hl_empty_bbo_messages_spot", 0
                    ),
                    "hl_quote_updates_spot": feed.counters.get("hl_quote_updates_spot", 0),
                    "queue_dropped": storage.dropped,
                    "simulation_queue_dropped": simulation.queue_dropped,
                    "simulation_depth_updates": feed.counters.get("simulation_depth_updates", 0),
                    "simulation_samples": sum(s["samples"] for s in simulation_view.values()),
                    "simulation_invalid": sum(s["invalid"] for s in simulation_view.values()),
                    "simulation_error": simulation.last_error,
                    "last_error": feed.last_error,
                }
                storage.emit("diagnostics/network_pipeline.jsonl", status)
                last_diag = loop_time
            if loop_time - last_persist >= runtime.persist_interval_seconds:
                storage.save_state(
                    "simulation/state.json",
                    {
                        "saved_at": time.time(),
                        "config": asdict(simulation_manager.current),
                        "score_config": asdict(score_manager.current),
                        "routes": simulation_view,
                        "queue_dropped": simulation.queue_dropped,
                    },
                )
                for route_id, history in histories.items():
                    if history.current:
                        route = history.current.route
                        safe = route_id.replace(":", "_").replace("/", "_")
                        storage.save_state(
                            f"{route.category}/states/{safe}.json",
                            {
                                "saved_at": time.time(),
                                "route": route_id,
                                "symbols": route.symbols,
                                "stats": history.to_dict(),
                                "latest": asdict(history.current),
                            },
                        )
                last_persist = loop_time
            if loop_time - last_render >= runtime.display_refresh_seconds:
                category = PAGES[page_index]
                page_valid = sum(
                    1
                    for route in routes
                    if route.category == category and route.route_id in valid_route_ids
                )
                hl_spot_quotes = sum(
                    1
                    for item in instruments
                    if item.venue == "hyperliquid"
                    and item.kind == "spot"
                    and (quote := quotes.get(item.key)) is not None
                    and 0 <= now - quote.received_mono <= runtime.quote_max_age_ms / 1000
                )
                status = f"WS活跃 {feed.counters.get('active_connections', 0)} | 建连累计 {feed.counters.get('connection_opens', 0)} | 重连 {feed.counters.get('reconnects', 0)} | 更新 {feed.counters.get('updates', 0)} | 当前页有效 {page_valid}/{category_counts.get(category, 0)} | 候选 SS/SP/PP {category_counts['spot_spot']}/{category_counts['spot_perp']}/{category_counts['perp_perp']} | HL现货有新鲜报价 {hl_spot_quotes} | H现×B永 {direction_counts.get('h_spot_b_perp', 0)}"
                price_min = market_manager.current.binance_price_min_usdt
                price_min_label = f"≥{price_min:g}" if price_min is not None else "不限"
                market_status = (
                    "发现/过成交量门槛（USDT/USDC范围）"
                    f" HL现货 {discovered_counts.get('hyperliquid_spot', 0)}/{qualified_counts.get('hyperliquid_spot', 0)}"
                    f"{'(缓存)' if ('hyperliquid', 'spot') in retained_categories else ''}"
                    f" HL永续 {discovered_counts.get('hyperliquid_perp', 0)}/{qualified_counts.get('hyperliquid_perp', 0)}"
                    f"{'(缓存)' if ('hyperliquid', 'perp') in retained_categories else ''}"
                    f" Binance现货 {discovered_counts.get('binance_spot', 0)}/{qualified_counts.get('binance_spot', 0)}"
                    f"{'(缓存)' if ('binance', 'spot') in retained_categories else ''}"
                    f" Binance永续 {discovered_counts.get('binance_perp', 0)}/{qualified_counts.get('binance_perp', 0)}"
                    f"{'(缓存)' if ('binance', 'perp') in retained_categories else ''}"
                    f" | Binance价格下限USDT {price_min_label}"
                )
                active_route_ids = {route.route_id for route in routes}
                render(
                    category,
                    {
                        route_id: history
                        for route_id, history in histories.items()
                        if route_id in active_route_ids
                    },
                    status + (f" | {feed.last_error}" if feed.last_error else ""),
                    {
                        **errors,
                        **{
                            m.path.name: m.error
                            for m in (simulation_manager, score_manager)
                            if m.error
                        },
                        **({"simulation": simulation.last_error} if simulation.last_error else {}),
                    },
                    stat_managers[category].current.top,
                    fresh_route_ids=valid_route_ids,
                    market_status=market_status,
                    excluded_route_ids=price_excluded_route_ids,
                    simulations=simulation_view,
                    simulation_config=simulation_manager.current,
                    score_config=score_manager.current,
                )
                last_render = loop_time
            if _read_k():
                page_index = (page_index + 1) % len(PAGES)
            await asyncio.sleep(max(0.02, runtime.sample_interval_ms / 1000))
    finally:
        feed.stop()
        simulation.stop()
        storage.stop()


def _read_k() -> bool:
    try:
        import msvcrt

        while msvcrt.kbhit():
            if msvcrt.getwch().lower() == "k":
                return True
    except ImportError:
        pass
    return False
