from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from ..paths import project_root

BASE_DIR = project_root()
CONFIG_DIR = BASE_DIR / "config"


@dataclass(frozen=True, slots=True)
class MarketConfig:
    quote_assets: tuple[str, ...] = ("USDT", "USDC")
    hyperliquid_min_spot_volume_usdt: float = 500_000
    hyperliquid_min_perp_volume_usdt: float = 500_000
    binance_min_spot_volume_usdt: float = 5_000_000
    binance_min_perp_volume_usdt: float = 5_000_000
    base_aliases: dict[str, str] | None = None
    hyperliquid_perp_quote_overrides: dict[str, str] | None = None

    def validate(self) -> None:
        if not self.quote_assets or any(q not in {"USDT", "USDC"} for q in self.quote_assets):
            raise ValueError("quote_assets 只支持 USDT 和 USDC")
        thresholds = (
            self.hyperliquid_min_spot_volume_usdt,
            self.hyperliquid_min_perp_volume_usdt,
            self.binance_min_spot_volume_usdt,
            self.binance_min_perp_volume_usdt,
        )
        if any(value < 0 for value in thresholds):
            raise ValueError("成交额门槛不能小于 0")
        for field_name in ("base_aliases", "hyperliquid_perp_quote_overrides"):
            value = getattr(self, field_name) or {}
            if not isinstance(value, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError(f"{field_name} 必须是字符串到字符串的 JSON 对象")


@dataclass(frozen=True, slots=True)
class StatisticsConfig:
    top: int = 40
    total_window_samples: int = 86_400
    long_window_samples: int = 28_800
    short_window_samples: int = 10_800
    quantile_low: float = 0.05
    quantile_high: float = 0.95
    sigma_multiplier: float = 2.0
    min_k_sigma_bps: float = 2.0
    open_threshold_bps: float = 7.0
    close_threshold_bps: float = -5.0

    def validate(self) -> None:
        if self.top < 1:
            raise ValueError("top 必须大于 0")
        if (
            not 2
            <= self.short_window_samples
            <= self.long_window_samples
            <= self.total_window_samples
        ):
            raise ValueError("窗口必须满足 2 <= short <= long <= total")
        if not 0 <= self.quantile_low < self.quantile_high <= 1:
            raise ValueError("分位数范围无效")
        if self.sigma_multiplier <= 0 or self.min_k_sigma_bps <= 0:
            raise ValueError("sigma_multiplier 和 min_k_sigma_bps 必须大于 0")
        if self.open_threshold_bps <= 0 or self.close_threshold_bps >= 0:
            raise ValueError("开仓阈值必须为正，平仓阈值必须为负")


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    market_refresh_seconds: float = 300.0
    sample_interval_ms: int = 1000
    display_refresh_seconds: float = 1.0
    quote_max_age_ms: int = 1000
    quote_match_tolerance_ms: int = 200
    raw_coalesce_interval_ms: int = 200
    binance_symbols_per_connection: int = 20
    hyperliquid_subscriptions_per_connection: int = 10
    persist_interval_seconds: float = 60.0
    quantile_refresh_seconds: float = 15.0
    network_log_max_bytes: int = 20_000_000
    data_directory: str = "data"

    def validate(self) -> None:
        if self.market_refresh_seconds < 30:
            raise ValueError("market_refresh_seconds 必须至少为 30 秒")
        if self.sample_interval_ms < 50:
            raise ValueError("sample_interval_ms 必须至少为 50 毫秒")
        if (
            self.display_refresh_seconds <= 0
            or self.persist_interval_seconds <= 0
            or self.quantile_refresh_seconds <= 0
        ):
            raise ValueError("刷新、分位数重算与持久化间隔必须大于 0")
        if (
            self.quote_max_age_ms < 1
            or not 0 <= self.quote_match_tolerance_ms <= self.quote_max_age_ms
        ):
            raise ValueError("行情年龄或行情接收时间差配置无效")
        if not 20 <= self.raw_coalesce_interval_ms <= 1000:
            raise ValueError("raw_coalesce_interval_ms 必须在 20 到 1000 毫秒之间")
        if not 1 <= self.binance_symbols_per_connection <= 200:
            raise ValueError("binance_symbols_per_connection 必须在 1 到 200 之间")
        if not 1 <= self.hyperliquid_subscriptions_per_connection <= 10:
            raise ValueError("hyperliquid_subscriptions_per_connection 必须在 1 到 10 之间")
        if self.network_log_max_bytes < 1_000_000 or not self.data_directory.strip():
            raise ValueError("网络日志上限至少 1MB，数据目录不能为空")


ConfigT = TypeVar("ConfigT")


class ConfigManager(Generic[ConfigT]):
    """读取并热加载 JSON 配置；无效修改保留上一份有效配置。"""

    def __init__(self, path: Path, config_type: type[ConfigT]) -> None:
        self.path = path
        self.config_type = config_type
        self.current = config_type()
        self.error = ""
        self._mtime_ns: int | None = None
        self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
        try:
            stat = self.path.stat()
            if not force and stat.st_mtime_ns == self._mtime_ns:
                return False
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("配置顶层必须是 JSON 对象")
            config = self.config_type(**raw)
            config.validate()  # type: ignore[attr-defined]
            self.current = config
            self._mtime_ns = stat.st_mtime_ns
            self.error = ""
            return True
        except Exception as exc:
            self.error = f"{self.path.name}: {type(exc).__name__}: {exc}"
            return False


def load_config_managers() -> tuple[ConfigManager[Any], ...]:
    return (
        ConfigManager(CONFIG_DIR / "market.json", MarketConfig),
        ConfigManager(CONFIG_DIR / "basis_spot_spot.json", StatisticsConfig),
        ConfigManager(CONFIG_DIR / "basis_spot_perp.json", StatisticsConfig),
        ConfigManager(CONFIG_DIR / "basis_perp_perp.json", StatisticsConfig),
        ConfigManager(CONFIG_DIR / "runtime.json", RuntimeConfig),
    )
