from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from math import ceil
from typing import Callable

import websockets

from ..domain.models import Instrument

HL_WS = "wss://api.hyperliquid.xyz/ws"
MAX_HL_CONNECTIONS = 10
SUBSCRIPTION_FAILURES_BEFORE_ISOLATION = 3


class _SubscriptionFailed(ConnectionError):
    def __init__(self, coin: str, reason: str) -> None:
        self.coin = coin
        super().__init__(f"BBO subscription for {coin} failed: {reason}")


class HyperliquidBboStream:
    """Hyperliquid public BBO subscriptions, isolated into recoverable shards."""

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
        self._isolated_coins: set[str] = set()
        self._subscription_failures: Counter[str] = Counter()
        self._universe_signature: frozenset[str] = frozenset()

    async def run(
        self, instruments: tuple[Instrument, ...], subscriptions_per_connection: int
    ) -> None:
        by_coin: dict[str, Instrument] = {}
        coins_by_kind: dict[str, list[str]] = {"spot": [], "perp": []}
        for instrument in instruments:
            by_coin[instrument.feed_coin] = instrument
            if instrument.kind == "spot":
                by_coin[instrument.symbol] = instrument
                by_coin[f"{instrument.base}/{instrument.quote}"] = instrument
            if instrument.kind in coins_by_kind:
                coins_by_kind[instrument.kind].append(instrument.feed_coin)

        for kind, coins in coins_by_kind.items():
            coins_by_kind[kind] = list(dict.fromkeys(coins))
        all_coins = frozenset(coin for coins in coins_by_kind.values() for coin in coins)
        if all_coins != self._universe_signature:
            # A changed market universe is a safe point to retry previously isolated coins.
            self._isolated_coins.clear()
            self._subscription_failures.clear()
            self._universe_signature = all_coins
        if not all_coins:
            return

        requested_size = max(1, min(subscriptions_per_connection, 10))
        shards = self._make_shards(coins_by_kind, requested_size)
        for key in tuple(self.counters):
            if key.startswith("hl_shard_"):
                self.counters.pop(key, None)
        self.counters["hl_shards_configured"] = len(shards)
        await asyncio.gather(
            *(
                self._hl_shard(kind, shard_index, shard, by_coin)
                for kind, shard_index, shard in shards
            )
        )

    @staticmethod
    def _make_shards(
        coins_by_kind: dict[str, list[str]], requested_size: int
    ) -> list[tuple[str, int, list[str]]]:
        """Keep spot/perp failures separate and stay within HL's 10-socket limit."""
        groups = [(kind, coins) for kind, coins in coins_by_kind.items() if coins]
        if not groups:
            return []

        # First honor the configured target. If that would exceed the documented
        # per-IP connection ceiling, distribute the maximum ten sockets evenly.
        shard_counts = {kind: ceil(len(coins) / requested_size) for kind, coins in groups}
        if sum(shard_counts.values()) > MAX_HL_CONNECTIONS:
            shard_counts = {kind: 1 for kind, _ in groups}
            while sum(shard_counts.values()) < MAX_HL_CONNECTIONS:
                kind, _ = max(
                    groups,
                    key=lambda group: len(group[1]) / shard_counts[group[0]],
                )
                shard_counts[kind] += 1

        result: list[tuple[str, int, list[str]]] = []
        for kind, coins in groups:
            shard_count = min(len(coins), shard_counts[kind])
            chunk_size, remainder = divmod(len(coins), shard_count)
            offset = 0
            for index in range(shard_count):
                size = chunk_size + (1 if index < remainder else 0)
                result.append((kind, index + 1, coins[offset : offset + size]))
                offset += size
        return result

    async def _hl_shard(
        self,
        kind: str,
        shard_index: int,
        coins: list[str],
        by_coin: dict[str, Instrument],
    ) -> None:
        error_key = f"Hyperliquid {kind} shard {shard_index}"
        metric_key = f"hl_shard_{kind}_{shard_index}"
        delay = 1.0
        while not self.stop_event.is_set():
            active_coins = [coin for coin in coins if coin not in self._isolated_coins]
            isolated_here = [coin for coin in coins if coin in self._isolated_coins]
            if not active_coins:
                self.set_error(
                    error_key,
                    "all BBO subscriptions in this shard are isolated after repeated failures: "
                    + ", ".join(isolated_here),
                )
                await asyncio.to_thread(self.stop_event.wait, 1.0)
                continue

            counted = False
            connected_at: float | None = None
            error_message = ""
            pending_coin: str | None = None
            try:
                async with websockets.connect(
                    HL_WS,
                    ping_interval=None,
                    close_timeout=3,
                    max_queue=16,
                    compression=None,
                ) as ws:
                    loop = asyncio.get_running_loop()
                    connected_at = loop.time()
                    self.counters["connection_opens"] += 1
                    self.counters["active_connections"] += 1
                    self.counters["hl_connection_opens"] += 1
                    self.counters["hl_active_connections"] += 1
                    counted = True
                    self.counters[f"{metric_key}_last_sent"] = 0
                    self.counters[f"{metric_key}_last_acks"] = 0

                    if isolated_here:
                        self.set_error(
                            error_key,
                            "shard is live; isolated repeatedly failing BBO coin(s): "
                            + ", ".join(isolated_here),
                        )
                    else:
                        self.clear_error(error_key)

                    connection_lost = asyncio.Event()
                    pong_received = asyncio.Event()
                    subscription_acks: asyncio.Queue[dict[str, object]] = asyncio.Queue()

                    async def subscribe(receiver_task: asyncio.Task[None]) -> None:
                        nonlocal pending_coin
                        for coin in active_coins:
                            if connection_lost.is_set() or ws.state.name != "OPEN":
                                return
                            pending_coin = coin
                            await ws.send(
                                json.dumps(
                                    {
                                        "method": "subscribe",
                                        "subscription": {"type": "bbo", "coin": coin},
                                    }
                                )
                            )
                            self.counters["hl_subscriptions_sent_total"] += 1
                            self.counters[f"{metric_key}_last_sent"] += 1

                            ack_task = asyncio.create_task(subscription_acks.get())
                            done, _ = await asyncio.wait(
                                (ack_task, receiver_task),
                                timeout=5,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if receiver_task in done:
                                ack_task.cancel()
                                await asyncio.gather(ack_task, return_exceptions=True)
                                try:
                                    receiver_task.result()
                                except Exception as exc:
                                    raise _SubscriptionFailed(
                                        coin, f"connection closed before ack: "
                                        f"{type(exc).__name__}: {exc}"
                                    ) from exc
                                raise _SubscriptionFailed(
                                    coin, "connection ended before subscription ack"
                                )
                            if ack_task not in done:
                                ack_task.cancel()
                                await asyncio.gather(ack_task, return_exceptions=True)
                                self.counters["hl_subscription_ack_timeouts"] += 1
                                raise _SubscriptionFailed(coin, "ack timed out after 5 seconds")

                            acknowledged = ack_task.result()
                            acknowledged_coin = acknowledged.get("coin")
                            if acknowledged_coin != coin:
                                raise _SubscriptionFailed(
                                    coin,
                                    f"ack mismatch; server acknowledged {acknowledged_coin!r}",
                                )
                            pending_coin = None
                            self._subscription_failures.pop(coin, None)
                            self.counters["hl_bbo_subscription_acks"] += 1
                            self.counters[f"{metric_key}_last_acks"] += 1
                            if coin != active_coins[-1]:
                                await asyncio.sleep(0.05)

                    async def receive() -> None:
                        nonlocal pending_coin
                        try:
                            async for raw in ws:
                                msg = json.loads(raw)
                                channel = msg.get("channel")
                                if channel == "error":
                                    self.counters["hl_subscription_errors"] += 1
                                    detail = str(msg.get("data", "unknown subscription error"))
                                    if pending_coin is not None:
                                        raise _SubscriptionFailed(
                                            pending_coin, f"server rejected subscription: {detail[:240]}"
                                        )
                                    self.set_error(error_key, f"subscription: {detail[:240]}")
                                    continue
                                if channel == "pong":
                                    self.counters["hl_pongs_received"] += 1
                                    pong_received.set()
                                    continue
                                if channel == "subscriptionResponse":
                                    response = msg.get("data", {})
                                    subscription = (
                                        response.get("subscription", {})
                                        if isinstance(response, dict)
                                        else {}
                                    )
                                    if subscription.get("type") == "bbo":
                                        subscription_acks.put_nowait(subscription)
                                    continue
                                if channel != "bbo":
                                    continue
                                data = msg.get("data", {})
                                inst = by_coin.get(str(data.get("coin", "")))
                                bbo = data.get("bbo")
                                if not inst:
                                    self.counters["hl_unmatched_bbo_messages"] += 1
                                    continue
                                self.counters[f"hl_bbo_messages_{inst.kind}"] += 1
                                if (
                                    not isinstance(bbo, list)
                                    or len(bbo) != 2
                                    or bbo[0] is None
                                    or bbo[1] is None
                                ):
                                    self.counters[f"hl_empty_bbo_messages_{inst.kind}"] += 1
                                    continue
                                try:
                                    bid, ask = float(bbo[0]["px"]), float(bbo[1]["px"])
                                    if self.publish(
                                        inst, bid, ask, int(data.get("time", 0)) or None
                                    ):
                                        self.counters[f"hl_quote_updates_{inst.kind}"] += 1
                                except (KeyError, TypeError, ValueError):
                                    self.counters["parse_errors"] += 1
                        finally:
                            connection_lost.set()

                    async def heartbeat() -> None:
                        while not self.stop_event.is_set() and not connection_lost.is_set():
                            await asyncio.sleep(20)
                            if (
                                not self.stop_event.is_set()
                                and not connection_lost.is_set()
                                and ws.state.name == "OPEN"
                            ):
                                pong_received.clear()
                                await ws.send(json.dumps({"method": "ping"}))
                                self.counters["hl_pings_sent"] += 1
                                try:
                                    await asyncio.wait_for(pong_received.wait(), timeout=10)
                                except TimeoutError as exc:
                                    self.counters["hl_ping_timeouts"] += 1
                                    await ws.close(code=1011, reason="Hyperliquid pong timeout")
                                    raise TimeoutError(
                                        "Hyperliquid JSON pong not received within 10 seconds"
                                    ) from exc

                    receiver_task = asyncio.create_task(receive())
                    heartbeat_task = asyncio.create_task(heartbeat())
                    try:
                        await subscribe(receiver_task)
                        done, _ = await asyncio.wait(
                            (receiver_task, heartbeat_task),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in done:
                            task.result()
                        if not receiver_task.done():
                            await receiver_task
                    finally:
                        receiver_task.cancel()
                        heartbeat_task.cancel()
                        await asyncio.gather(
                            receiver_task, heartbeat_task, return_exceptions=True
                        )
            except asyncio.CancelledError:
                self.clear_error(error_key)
                raise
            except _SubscriptionFailed as exc:
                failures = self._subscription_failures[exc.coin] + 1
                self._subscription_failures[exc.coin] = failures
                error_message = f"{type(exc).__name__}: {exc} ({failures}/"
                error_message += f"{SUBSCRIPTION_FAILURES_BEFORE_ISOLATION})"
                if failures >= SUBSCRIPTION_FAILURES_BEFORE_ISOLATION:
                    self._isolated_coins.add(exc.coin)
                    self.counters["hl_isolated_subscriptions_total"] += 1
                    error_message += "; isolated this coin so sibling subscriptions can recover"
            except Exception as exc:
                error_message = f"{type(exc).__name__}: {exc}"
            finally:
                if counted:
                    self.counters["active_connections"] -= 1
                    self.counters["hl_active_connections"] -= 1
                if connected_at is not None:
                    uptime_ms = int((asyncio.get_running_loop().time() - connected_at) * 1000)
                    self.counters["hl_last_connection_uptime_ms"] = uptime_ms
                    self.counters[f"{metric_key}_last_uptime_ms"] = uptime_ms

            if self.stop_event.is_set():
                break
            loop = asyncio.get_running_loop()
            if connected_at is not None and loop.time() - connected_at >= 60:
                delay = 1.0
            self.set_error(error_key, error_message or "connection closed; reconnecting")
            self.counters["reconnects"] += 1
            self.counters["hl_reconnects"] += 1
            await asyncio.to_thread(self.stop_event.wait, delay)
            delay = min(delay * 2, 30)
