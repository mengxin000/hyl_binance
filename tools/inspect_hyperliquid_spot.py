from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "market.json"
INFO_URL = "https://api.hyperliquid.xyz/info"


def _volume(context: dict) -> str:
    try:
        return f"{float(context.get('dayNtlVlm', 0) or 0):,.2f}"
    except (TypeError, ValueError):
        return str(context.get("dayNtlVlm"))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="打印 Hyperliquid 返回的 BTC/UBTC 现货元数据")
    parser.add_argument(
        "--direct",
        action="store_true",
        help="忽略 HTTP_PROXY/HTTPS_PROXY 环境变量，直接连接 API",
    )
    args = parser.parse_args()

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取配置失败 {CONFIG_PATH}: {exc}")
        config = {}

    aliases = config.get("base_aliases", {}) or {}
    alias = aliases.get("hyperliquid:UBTC", aliases.get("UBTC", "UBTC"))
    print(f"请求 Hyperliquid 公共接口: {INFO_URL}")
    print(f"配置别名: hyperliquid:UBTC -> {alias}")

    session = requests.Session()
    if args.direct:
        session.trust_env = False
    try:
        response = session.post(
            INFO_URL,
            json={"type": "spotMetaAndAssetCtxs"},
            headers={"Content-Type": "application/json"},
            timeout=(5, 30),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        print(f"请求失败: {type(exc).__name__}: {exc}")
        print("请在能访问 api.hyperliquid.xyz 的 PowerShell 环境中重试。")
        return 1
    except ValueError as exc:
        print(f"响应不是有效 JSON: {exc}")
        return 1
    finally:
        session.close()

    if not isinstance(payload, list) or len(payload) != 2:
        print(f"响应结构与预期不符: {type(payload).__name__}")
        return 1

    spot_meta, contexts = payload
    tokens = {
        int(token["index"]): token
        for token in spot_meta.get("tokens", [])
        if token.get("index") is not None
    }
    token_ids_by_name: dict[str, set[str]] = {}
    for token in tokens.values():
        name = str(token.get("name", "")).upper()
        token_ids_by_name.setdefault(name, set()).add(str(token.get("tokenId", "")))

    print(
        f"元数据 token 数: {len(tokens)} | spot universe 市场数: {len(spot_meta.get('universe', []))}"
    )
    print("\n相关 token 元数据:")
    for token in tokens.values():
        name = str(token.get("name", "")).upper()
        if name in {"BTC", "UBTC", "USDC"}:
            print(
                f"  token_index={token.get('index')} name={name} "
                f"tokenId={token.get('tokenId')} isCanonical={token.get('isCanonical')}"
            )

    print("\nBTC/UBTC 相关现货市场（volume 为 HL dayNtlVlm）:")
    found = 0
    universe = spot_meta.get("universe", [])
    for position, market in enumerate(universe):
        token_indices = market.get("tokens", [])
        if len(token_indices) != 2:
            continue
        try:
            base_token = tokens[int(token_indices[0])]
            quote_token = tokens[int(token_indices[1])]
        except (KeyError, TypeError, ValueError):
            continue

        base = str(base_token.get("name", "")).upper()
        quote = str(quote_token.get("name", "")).upper()
        if base not in {"BTC", "UBTC"} and quote not in {"BTC", "UBTC"}:
            continue

        found += 1
        context = contexts[position] if position < len(contexts) else {}
        raw_market_index = market.get("index")
        market_index = int(raw_market_index if raw_market_index is not None else position)
        raw_market_name = market.get("name", f"@{market_index}")
        feed_coin = "PURR/USDC" if (base, quote) == ("PURR", "USDC") else f"@{market_index}"
        base_duplicate = len(token_ids_by_name.get(base, ())) > 1
        quote_duplicate = len(token_ids_by_name.get(quote, ())) > 1
        canonical_base = aliases.get(f"hyperliquid:{base}", aliases.get(base, base))
        index_context = contexts[market_index] if market_index < len(contexts) else {}

        print(f"  API market name : {raw_market_name}")
        print(f"  token pair      : {base}/{quote}")
        print(f"  market index    : {market_index} (universe position {position})")
        print(f"  BBO feed coin   : {feed_coin}")
        print(
            f"  context[position={position}]: dayNtlVlm={_volume(context)}, "
            f"midPx={context.get('midPx')}, markPx={context.get('markPx')}, prevDayPx={context.get('prevDayPx')}"
        )
        if market_index != position:
            print(
                f"  context[index={market_index}]   : dayNtlVlm={_volume(index_context)}, "
                f"midPx={index_context.get('midPx')}, markPx={index_context.get('markPx')}, "
                f"prevDayPx={index_context.get('prevDayPx')}"
            )
        print(f"  base tokenId    : {base_token.get('tokenId')} (name duplicate={base_duplicate})")
        print(
            f"  quote tokenId   : {quote_token.get('tokenId')} (name duplicate={quote_duplicate})"
        )
        print(f"  canonical base  : {canonical_base}")
        print(f"  old duplicate guard would drop: base={base_duplicate}, quote={quote_duplicate}")
        print()

    if not found:
        print("没有找到 base token 名为 BTC 或 UBTC 的 spot universe 市场。")
        print("这时请检查返回的 token/market 元数据，不能用前端显示名代替 API 原始名称。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
