# -*- coding=utf-8 -*-
# @TIME : 2026/10/10 15:11
# @Author :mengxin
# @File:us_info.py
# @Software:PyCharm
import requests, json

r = requests.post(
    "https://api.hyperliquid.xyz/info",
    json={"type": "meta"},
    timeout=10
)
data = r.json()

for m in data["universe"]:
    # print(m["name"], "| maxLeverage:", m["maxLeverage"], "| szDecimals:", m["szDecimals"])
    print(m)