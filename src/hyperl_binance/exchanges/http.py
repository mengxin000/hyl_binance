from __future__ import annotations

import time
from typing import Any

import requests

BINANCE_SPOT_HOSTS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
)
BINANCE_FUTURES_HOSTS = (
    "https://fapi.binance.com",
)


def get_json(session: requests.Session, url: str) -> Any:
    response = session.get(url, timeout=(5, 15))
    response.raise_for_status()
    return response.json()


def get_binance_json(session: requests.Session, hosts: tuple[str, ...], path: str) -> Any:
    """Try configured Binance REST hosts on transport/TLS failures."""
    errors: list[str] = []
    for host in hosts:
        try:
            return get_json(session, host + path)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in (418, 429) or 400 <= status < 500:
                raise
            errors.append(f"{host}: HTTP {status}")
        except requests.RequestException as exc:
            errors.append(f"{host}: {type(exc).__name__}: {exc}")
    raise requests.ConnectionError("所有配置的 Binance REST 域名均请求失败；" + " | ".join(errors))


def hl_info(session: requests.Session, request: dict[str, Any]) -> Any:
    for attempt in range(3):
        try:
            response = session.post(
                "https://api.hyperliquid.xyz/info", json=request, timeout=(5, 30)
            )
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == 2:
                raise
            time.sleep(0.25 * (attempt + 1))


def optional_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None
