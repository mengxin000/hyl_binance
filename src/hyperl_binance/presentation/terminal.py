from __future__ import annotations

import shutil
import sys
import time
import unicodedata
from collections.abc import Callable

from ..config.settings import StatisticsConfig
from ..domain.models import Category, Instrument, SpreadRoute
from ..domain.statistics import RouteHistory
from ..simulation.config import ScoreConfig, SimulationConfig
from ..simulation.score import score_routes

PAGES: tuple[Category, ...] = ("spot_spot", "spot_perp", "perp_perp")
TITLES = {"spot_spot": "现货 / 现货", "spot_perp": "现货 / 永续", "perp_perp": "永续 / 永续"}


def _width(value: str) -> int:
    """Terminal display width (CJK full-width glyphs occupy two cells)."""
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def _clip(value: str, width: int) -> str:
    result = ""
    used = 0
    for char in value:
        cells = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + cells > width:
            break
        result += char
        used += cells
    if len(result) < len(value) and width >= 1:
        while used >= width and result:
            removed = result[-1]
            result = result[:-1]
            used -= 2 if unicodedata.east_asian_width(removed) in {"W", "F"} else 1
        result += "…"
        used += 1
    return result + " " * max(0, width - used)


def _cell(value: str, width: int, align: str = "left") -> str:
    value = _clip(value, width).rstrip()
    padding = max(0, width - _width(value))
    if align == "right":
        return " " * padding + value
    if align == "center":
        left = padding // 2
        return " " * left + value + " " * (padding - left)
    return value + " " * padding


def _volume(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K"
    return f"{value:.2f}"


def _route_name(route: SpreadRoute) -> str:
    def leg(instrument: Instrument) -> str:
        venue = "h" if instrument.venue == "hyperliquid" else "b"
        return f"{venue}_{route.base}{instrument.quote}"

    instruments = (route.hyperliquid, route.binance)
    if route.category == "spot_perp":
        instruments = tuple(sorted(instruments, key=lambda item: 0 if item.kind == "spot" else 1))
    return f"{leg(instruments[0])}/{leg(instruments[1])}"


def _turnover(route: SpreadRoute, kind: str) -> str:
    instruments = [item for item in (route.hyperliquid, route.binance) if item.kind == kind]
    if not instruments:
        return "—"
    return "/".join(_volume(item.quote_volume) for item in instruments)


def _market_price(history: RouteHistory, kind: str) -> str:
    snapshot = history.current
    if snapshot is None:
        return "—"
    route = snapshot.route
    values = []
    for instrument, price in (
        (route.hyperliquid, snapshot.hyperliquid_mid_usdt),
        (route.binance, snapshot.binance_mid_usdt),
    ):
        if instrument.kind == kind:
            venue = "h" if instrument.venue == "hyperliquid" else "b"
            values.append(f"{venue}:{price:.8g}")
    return "/".join(values) if values else "—"


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 10_000:+.2f}"


def _funding_pair(hl: float | None, binance: float | None, route: SpreadRoute) -> str:
    hl_perp = route.hyperliquid.kind == "perp"
    binance_perp = route.binance.kind == "perp"
    if hl_perp and binance_perp:
        return f"{_rate(hl)}/{_rate(binance)}"
    if hl_perp:
        return _rate(hl)
    if binance_perp:
        return _rate(binance)
    else:
        return "—"


def _cols(page: Category) -> list[tuple[int, str, str, str, Callable[[int, RouteHistory], str]]]:
    route_title = TITLES[page]
    columns = [
        (3, "#", "", "right", lambda n, h: str(n)),
        (24, route_title, "路线", "left", lambda n, h: _route_name(h.current.route)),
    ]
    if page == "spot_perp":
        columns.extend(
            [
                (
                    14,
                    "现货成交额",
                    "24h成交额 · USDT",
                    "right",
                    lambda n, h: _turnover(h.current.route, "spot"),
                ),
                (
                    14,
                    "永续成交额",
                    "24h成交额 · USDT",
                    "right",
                    lambda n, h: _turnover(h.current.route, "perp"),
                ),
                (
                    15,
                    "现货价格 · USDT",
                    "现货 mid · USDT",
                    "right",
                    lambda n, h: _market_price(h, "spot"),
                ),
            ]
        )
    elif page == "spot_spot":
        columns.extend(
            [
                (
                    17,
                    "现货成交额 H/B",
                    "24h成交额 · USDT",
                    "right",
                    lambda n, h: _turnover(h.current.route, "spot"),
                ),
                (
                    25,
                    "现货价格 H/B · USDT",
                    "现货 mid · USDT",
                    "right",
                    lambda n, h: _market_price(h, "spot"),
                ),
            ]
        )
    else:
        columns.extend(
            [
                (
                    17,
                    "永续成交额 H/B",
                    "24h成交额 · USDT",
                    "right",
                    lambda n, h: _turnover(h.current.route, "perp"),
                ),
                (
                    25,
                    "永续价格 H/B · USDT",
                    "永续 mid · USDT",
                    "right",
                    lambda n, h: _market_price(h, "perp"),
                ),
            ]
        )
    basis_label = {
        "spot_spot": "跨所价差统计 · bps",
        "spot_perp": "现货永续基差 · bps",
        "perp_perp": "跨所永续价差 · bps",
    }[page]
    columns.extend(
        [
            (10, "μ", basis_label, "right", lambda n, h: f"{h.stats.mean_bps:+.2f}"),
            (
                21,
                "μ±2σ",
                basis_label,
                "right",
                lambda n, h: (
                    f"{h.stats.mean_bps - 2 * h.stats.sigma_bps:+.2f}~{h.stats.mean_bps + 2 * h.stats.sigma_bps:+.2f}"
                ),
            ),
            (
                18,
                "P5~P95短窗",
                f"{basis_label}分布",
                "right",
                lambda n, h: f"{h.stats.p_low_short_bps:+.2f}~{h.stats.p_high_short_bps:+.2f}",
            ),
            (
                18,
                "P5~P95长窗",
                f"{basis_label}分布",
                "right",
                lambda n, h: f"{h.stats.p_low_long_bps:+.2f}~{h.stats.p_high_long_bps:+.2f}",
            ),
            (
                21,
                "运行范围",
                basis_label,
                "right",
                lambda n, h: f"{h.stats.low_bps:+.2f}~{h.stats.high_bps:+.2f}",
            ),
            (11, "当前价差", "基差 · bps", "right", lambda n, h: f"{h.current.spread_bps:+.2f}"),
        ]
    )
    if page != "spot_spot":
        funding_suffix = " H/B" if page == "perp_perp" else ""
        columns.extend(
            [
                (
                    19,
                    f"当前资费{funding_suffix}",
                    "资费 · bps",
                    "right",
                    lambda n, h: _funding_pair(
                        h.current.funding_hl, h.current.funding_binance, h.current.route
                    ),
                ),
            ]
        )
    columns.extend(
        [
            (
                20,
                "HL/B/差",
                "报价年龄 · ms",
                "right",
                lambda n, h: (
                    f"{h.current.hyperliquid_age_ms:.2f}/{h.current.binance_age_ms:.2f}/{h.current.receive_skew_ms:.2f}"
                ),
            ),
        ]
    )
    return columns


def _header(
    columns: list[tuple[int, str, str, str, Callable[[int, RouteHistory], str]]],
) -> list[str]:
    header = "  ".join(
        _cell(title, width, align) for width, title, _group, align, _format in columns
    )
    separator = "─" * (sum(width for width, *_ in columns) + 2 * (len(columns) - 1))
    return [header, separator]


def render(
    page: Category,
    histories: dict[str, RouteHistory],
    network_status: str,
    errors: dict[str, str],
    top: int = 40,
    fresh_route_ids: set[str] | None = None,
    market_status: str = "",
    excluded_route_ids: set[str] | None = None,
    simulations: dict[str, dict] | None = None,
    simulation_config: SimulationConfig | None = None,
    score_config: ScoreConfig | None = None,
    statistics_config: StatisticsConfig | None = None,
) -> None:
    fresh_route_ids = fresh_route_ids or set()
    excluded_route_ids = excluded_route_ids or set()
    simulations = simulations or {}
    rows = [
        h
        for h in histories.values()
        if h.current
        and h.current.route.category == page
        and h.current.route.route_id not in excluded_route_ids
        and h.config.sigma_multiplier * h.stats.sigma_bps >= h.config.min_k_sigma_bps
    ]
    scores = score_routes(
        simulations,
        {h.current.route.route_id for h in rows},
        score_config
        if simulation_config and simulation_config.enabled and score_config
        else ScoreConfig(enabled=False),
        simulation_config.min_samples if simulation_config else 5,
        time.monotonic_ns(),
    )

    def sort_key(history):
        rid = history.current.route.route_id
        result = simulations.get(rid, {})
        profit = result.get("profit_index")
        return (
            -int(rid in scores),
            -scores.get(rid, 0),
            -int(profit is not None),
            -(profit if profit is not None else 0),
            -result.get("paired_samples", 0),
            -result.get("samples", 0),
            _route_name(history.current.route),
        )

    rows.sort(key=sort_key)
    columns = _cols(page)
    if simulation_config and simulation_config.enabled:

        def observation(history):
            return simulations.get(history.current.route.route_id, {})

        def metric(history, field):
            value = observation(history).get(field)
            return "—" if value is None else f"{value:+.2f}"

        def trigger_lines(history):
            trigger = observation(history).get("trigger") or {}
            if trigger.get("status") == "warming_up":
                return f"预热{trigger['sample_count']}/{trigger['min_samples']}"
            if trigger.get("status") == "flat_distribution":
                return "分布零宽"
            if trigger.get("status") != "ready":
                return "—"
            return f"{trigger['open_bps']:+.2f}/{trigger['close_bps']:+.2f}"

        columns.extend(
            [
                (
                    22,
                    "开线/平线·bps",
                    "实际触发线",
                    "right",
                    lambda n, h: trigger_lines(h),
                ),
                (
                    14,
                    "模拟开/平/异常",
                    "滑点观测",
                    "right",
                    lambda n, h: (
                        f"{observation(h).get('open_samples', 0)}/"
                        f"{observation(h).get('close_samples', 0)}/"
                        f"{observation(h).get('invalid', 0)}"
                    ),
                ),
                (
                    18,
                    "延迟/深度·bps",
                    "滑点观测",
                    "right",
                    lambda n, h: metric(h, "mean_delay_bps") + "/" + metric(h, "mean_depth_bps"),
                ),
                (
                    18,
                    "滑点均值/P95",
                    "滑点观测·bps",
                    "right",
                    lambda n, h: (
                        metric(h, "mean_total_slippage_bps") + "/" + metric(h, "p95_slippage_bps")
                    ),
                ),
                (8, "费用·bps", "模拟手续费", "right", lambda n, h: metric(h, "mean_fee_bps")),
                (
                    11,
                    "开edge·bps",
                    "模拟净edge",
                    "right",
                    lambda n, h: metric(h, "mean_open_net_edge_bps"),
                ),
                (
                    11,
                    "平edge·bps",
                    "模拟净edge",
                    "right",
                    lambda n, h: metric(h, "mean_close_net_edge_bps"),
                ),
                (
                    12,
                    "Profit指数",
                    "累计净edge指数",
                    "right",
                    lambda n, h: metric(h, "profit_index"),
                ),
                (
                    8,
                    "Score",
                    "相对评分",
                    "right",
                    lambda n, h: (
                        f"{scores[h.current.route.route_id]:.2f}"
                        if h.current.route.route_id in scores
                        else "—"
                    ),
                ),
            ]
        )
    title = "HYPERLIQUID × BINANCE | 公共行情价差筛选器"
    lines = [
        title,
        f"页面：{TITLES[page]}  |  K 切换页面  |  Ctrl+C 退出",
        f"行情：{network_status}",
    ]
    if market_status:
        lines.append(f"市场：{market_status}")
    if errors:
        lines.append("发现错误：" + " | ".join(f"{k}={v}" for k, v in list(errors.items())[:3]))

    terminal_width = shutil.get_terminal_size(fallback=(180, 24)).columns
    blocks = []
    start, used = 0, 0
    for index, (width, *_rest) in enumerate(columns):
        budget = terminal_width - (4 if start else 0)
        additional = width + (2 if index > start else 0)
        if used + additional > budget and index > start:
            blocks.append((start, index))
            start, used = index, width
        else:
            used += additional
    blocks.append((start, len(columns)))
    for start, end in blocks:
        lines.extend(("    " if start else "") + line for line in _header(columns[start:end]))

    for n, history in enumerate(rows[:top], 1):
        cells = [
            formatter(n, history)
            for _width_value, _header_text, _group, _align, formatter in columns
        ]
        if history.current.route.route_id not in fresh_route_ids:
            cells[0] = f"~{n}"
        for start, end in blocks:
            line = "  ".join(
                _cell(cells[index], width, align)
                for index, (width, _header_text, _group, align, _format) in enumerate(
                    columns[start:end], start
                )
            )
            lines.append(("    " if start else "") + line)

    if not rows:
        page_histories = [
            h
            for h in histories.values()
            if h.current
            and h.current.route.category == page
            and h.current.route.route_id not in excluded_route_ids
        ]
        if page_histories:
            lines.append("当前页路线尚未达到配置的 2σ 波动筛选门槛。")
        else:
            lines.append("当前页暂无同时满足成交额、配对与行情新鲜度条件的路线。")
    lines.append(
        f"刷新时间：{time.strftime('%H:%M:%S')}  |  成交额为 24h 统计；资费显示交易所公布费率，换算为 bps"
    )
    lines.append("榜内 ~序号表示本轮报价不新鲜，数值为最后一次有效快照；不计入“当前页有效”。")
    if simulation_config and simulation_config.enabled:
        if statistics_config:
            cfg = statistics_config
            window = {"total": "总窗", "long": "长窗", "short": "短窗"}[cfg.threshold_window]
            if cfg.threshold_mode == "fixed":
                rule = f"开 > μ{cfg.open_threshold_bps:+g}，平 < μ{cfg.close_threshold_bps:+g}；回到μ复位"
                warmup = 2
            elif cfg.threshold_mode == "sigma":
                rule = (
                    f"开 > μ+{cfg.open_threshold_sigma:g}σ，"
                    f"平 < μ-{cfg.close_threshold_sigma:g}σ；回到μ复位"
                )
                warmup = cfg.threshold_min_samples
            else:
                rule = (
                    f"开 > P{cfg.open_threshold_quantile * 100:g}，"
                    f"平 < P{cfg.close_threshold_quantile * 100:g}；回到P50复位"
                )
                warmup = cfg.threshold_min_samples
            lines.append(
                f"模拟触发 {cfg.threshold_mode}/{window} | {rule} | "
                f"至少 {warmup} 个有效行情统计样本；修改触发规则重置对应模拟结果。"
            )
        lines.append(
            f"滑点模拟 {simulation_config.order_notional_usdt:g}U | "
            f"maker假定成交 {simulation_config.exchange_delay_ms:g}ms，"
            f"taker追加延迟 {simulation_config.deal_delay_ms:g}ms | "
            f"开、平各至少 {simulation_config.min_samples} 个有效样本评分；异常不计入均值。"
        )
        lines.append(
            f"模拟开/平/异常及 edge 统计最近 {simulation_config.window_samples} 次已完成模拟；"
            "Profit指数 = min(开,平) × (开edge + 平edge)，非 USDT 利润或年化收益。"
        )
        lines.append(
            "滑点正值为成本、负值为改善；净edge已扣手续费。Score 为本页相对评分，"
            "未评分时按 Profit指数排序；缺少任一方向时指数显示 —。"
        )
        if score_config and score_config.enabled:
            weight = score_config.profit_weight + score_config.slippage_weight
            lines.append(
                f"Score: Profit排名 {score_config.profit_weight / weight:.0%} + "
                f"滑点风险排名 {score_config.slippage_weight / weight:.0%}，"
                f"乘可评估覆盖率和 min(1,配对次数/{score_config.confidence_paired_samples})；"
                f"任一方向超过 {score_config.max_sample_age_seconds:g}s 无有效样本不评分，"
                "Profit≤0 时 Score=0。"
            )
    if page == "spot_perp":
        lines.append(
            "现货/永续基差 = 折算为 USDT 后的永续 ask / 现货 bid - 1；成交额按市场报价币计。"
        )
    else:
        lines.append("价差 = HL mid / Binance mid - 1（均折算为 USDT）；成交额按市场报价币计。")
    screen = "\n".join(lines)
    if sys.stdout.isatty():
        # Clear and repaint through one terminal write. Spawning `cls`/`clear`
        # on every frame leaves a visible blank interval and causes flicker.
        screen = "\x1b[2J\x1b[H" + screen
    sys.stdout.write(screen + "\n")
    sys.stdout.flush()
