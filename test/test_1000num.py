# -*- coding=utf-8 -*-
# @TIME : 2026/10/10 15:58
# @Author :mengxin
# @File:test_1000num.py
# @Software:PyCharm
import requests

r = requests.post(
    "https://api.hyperliquid.xyz/info",
    json={"type": "metaAndAssetCtxs"},
    timeout=10
)
data = r.json()
universe = data[0]["universe"]
ctxs = data[1]

threshold = 10_000_000
count = 0
for i, m in enumerate(universe):
    ctx = ctxs[i]
    vlm = float(ctx["dayNtlVlm"])
    if vlm > threshold:
        count += 1
        print(f"{m['name']:10s} {vlm:>18,.0f}")

print(f"\n共 {count} 个合约 dayNtlVlm > {threshold:,}")