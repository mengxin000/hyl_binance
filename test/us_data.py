# -*- coding=utf-8 -*-
# @TIME : 2026/10/10 15:13
# @Author :mengxin
# @File:us_data.py
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

for i, m in enumerate(universe):
    ctx = ctxs[i]
    print(
        m["name"],
        "| markPx:", ctx["markPx"],
        "| midPx:", ctx["midPx"],
        "| funding:", ctx["funding"],
        "| openInterest:", ctx["openInterest"],
        "| dayNtlVlm:", ctx["dayNtlVlm"],
    )