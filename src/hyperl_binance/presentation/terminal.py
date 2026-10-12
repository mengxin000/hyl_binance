from __future__ import annotations

import shutil
import sys
import time
import unicodedata
from collections.abc import Callable

from ..domain.models import Category, Instrument, SpreadRoute
from ..domain.statistics import RouteHistory

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
                9,
                "开/平",
                "机会",
                "right",
                lambda n, h: f"{h.stats.open_count}/{h.stats.close_count}",
            ),
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
) -> None:
    fresh_route_ids = fresh_route_ids or set()
    excluded_route_ids = excluded_route_ids or set()
    rows = [
        h
        for h in histories.values()
        if h.current
        and h.current.route.category == page
        and h.current.route.route_id not in excluded_route_ids
        and h.config.sigma_multiplier * h.stats.sigma_bps >= h.config.min_k_sigma_bps
    ]
    rows.sort(
        key=lambda h: (
            min(h.stats.open_count, h.stats.close_count),
            h.stats.open_count + h.stats.close_count,
        ),
        reverse=True,
    )
    columns = _cols(page)
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
    full_width = sum(width for width, *_ in columns) + 2 * (len(columns) - 1)
    split = full_width > terminal_width
    split_at = 8 if split else len(columns)
    lines.extend(_header(columns[:split_at]))
    if split:
        lines.extend(_header(columns[split_at:]))

    for n, history in enumerate(rows[:top], 1):
        cells = [
            formatter(n, history)
            for _width_value, _header_text, _group, _align, formatter in columns
        ]
        if history.current.route.route_id not in fresh_route_ids:
            cells[0] = f"~{n}"
        first = "  ".join(
            _cell(cells[index], width, align)
            for index, (width, _header_text, _group, align, _format) in enumerate(
                columns[:split_at]
            )
        )
        lines.append(first)
        if split:
            continuation = "  ".join(
                _cell(cells[index], width, align)
                for index, (width, _header_text, _group, align, _format) in enumerate(
                    columns[split_at:], split_at
                )
            )
            lines.append("    " + continuation)

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
