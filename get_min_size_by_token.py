
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
get_min_size_by_token.py
------------------------
不下单，直接通过 Gamma API 用 CLOB token_id 查询市场，并读取 “最小份数（shares）”。
输出 JSON：{ token_id, market_id, slug, min_size_shares, min_notional_usd, source }

用法：
  python3 get_min_size_by_token.py --token-id <CLOB_TOKEN_ID>
  # 或
  python3 get_min_size_by_token.py <CLOB_TOKEN_ID>
"""
import argparse
import json
import sys
import requests
from typing import Any, Dict, Optional

GAMMA_ENDPOINTS = [
    "https://gamma-api.polymarket.com/markets",
    "https://gamma.polymarket.com/markets",
]

def _to_int(x) -> Optional[int]:
    try:
        f = float(x)
        return int(round(f))
    except Exception:
        return None

def _first_market_by_token(token_id: str) -> Optional[Dict[str, Any]]:
    params_key = "clob_token_ids"  # 正确参数名：用 CLOB token_id 过滤
    headers = {"User-Agent": "min-shares-fetcher/1.0"}
    for base in GAMMA_ENDPOINTS:
        url = f"{base}?{params_key}={token_id}"
        try:
            r = requests.get(url, timeout=10, headers=headers)
            if r.ok:
                js = r.json()
                if isinstance(js, list) and js:
                    return js[0]
        except Exception:
            pass
    return None

def _extract_min_shares(market: Dict[str, Any]) -> Optional[int]:
    # 常见字段名
    candidates = [
        "orderMinSize",
        "order_min_size",
        "orderMin",
        "order_min",
        "minimumOrderSize",
        "minimum_order_size",
    ]
    for k in candidates:
        if k in market:
            v = market.get(k)
            ms = _to_int(v)
            if ms and ms > 0:
                return ms
    # 有些可能嵌套，简单向下再找一层
    for v in market.values():
        if isinstance(v, dict):
            for k in candidates:
                if k in v:
                    ms = _to_int(v.get(k))
                    if ms and ms > 0:
                        return ms
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("token_id", nargs="?", help="CLOB token_id（YES/NO 子问题的 token）")
    ap.add_argument("--token-id", dest="token_id_opt", help="同上：CLOB token_id")
    args = ap.parse_args()

    token_id = args.token_id or args.token_id_opt
    if not token_id:
        print("usage: get_min_size_by_token.py (--token-id TOKEN_ID | TOKEN_ID)", file=sys.stderr)
        sys.exit(2)

    market = _first_market_by_token(token_id)
    if not market:
        out = {
            "token_id": token_id,
            "error": "NOT_FOUND_IN_GAMMA_BY_clob_token_ids",
            "hint": "稍后重试，或确认该 token_id 是否为 CLOB 子问题 token。"
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(1)

    min_shares = _extract_min_shares(market)
    out = {
        "token_id": token_id,
        "market_id": market.get("id"),
        "slug": market.get("slug"),
        "min_size_shares": min_shares,
        "min_notional_usd": 1,   # CLOB 名义额门槛（price * size ≥ 1 USD）
        "source": "gamma?clob_token_ids"
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
