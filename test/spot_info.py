# -*- coding=utf-8 -*-
# @TIME : 2026/10/10 15:06
# @Author :mengxin
# @File:test_info.py
# @Software:PyCharm
import requests, json

r = requests.post(
    "https://api.hyperliquid.xyz/info",
    json={"type": "spotMeta"},
    timeout=10
)
print(json.dumps(r.json(), indent=2, ensure_ascii=False))