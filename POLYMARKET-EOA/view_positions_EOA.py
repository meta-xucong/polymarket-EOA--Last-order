#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
view_positions_EOA.py
---------------------
EOA优先的 Polymarket 持仓查看脚本：
- 直接调用官方 Data-API 的 /positions （不会再误打 Gamma 的 /wallets/... 之类无效路径）。
- “用户地址(user)”即传入你的 **EOA 地址**（或你在站内的 Proxy，也同样兼容）。
- 若你只用 EOA 直连、并且没生成站内 Proxy，本脚本仍然能查到（以 EOA 地址为 user）。

环境变量（优先级从高到低）：
- POLY_EOA_ADDRESS / POLY_ADDRESS / POLY_WALLET
- 或提供私钥（POLY_EOA_KEY / POLY_KEY / POLY_PRIVATE_KEY / PRIVATE_KEY）以自动推导地址
- 可选：DATA_API_HOST（默认 https://data-api.polymarket.com）

依赖：requests、eth_account（若走私钥推导地址）。

用法：
    python3 view_positions_EOA.py
    python3 view_positions_EOA.py --json
"""

import os
import sys
import json
import datetime as dt
from typing import Optional, Dict, Any, List, Set

import requests

DATA_API_HOST = os.environ.get("DATA_API_HOST", "https://data-api.polymarket.com").rstrip("/")

_ADDR_ENV_CANDIDATES = [
    "POLY_EOA_ADDRESS",
    "POLY_ADDRESS",
    "POLY_WALLET",
]

_KEY_ENV_CANDIDATES = [
    "POLY_EOA_KEY",
    "POLY_KEY",
    "POLY_PRIVATE_KEY",
    "PRIVATE_KEY",
]


def _derive_address_from_key(raw_key: str) -> Optional[str]:
    raw = raw_key.strip()
    if raw.startswith(("0x", "0X")):
        raw = raw[2:]
    if len(raw) != 64:
        return None
    try:
        from eth_account import Account  # type: ignore
    except Exception:
        return None
    try:
        acct = Account.from_key(bytes.fromhex(raw))
        return acct.address
    except Exception:
        return None


def _infer_wallet_address() -> Optional[str]:
    # 明确地址优先
    for env_name in _ADDR_ENV_CANDIDATES:
        v = os.getenv(env_name, "").strip()
        if v:
            if v.startswith("0x") and len(v) == 42:
                return "0x" + v[2:].lower()
            return v

    # 其次尝试私钥推导
    for env_name in _KEY_ENV_CANDIDATES:
        k = os.getenv(env_name, "").strip()
        if k:
            addr = _derive_address_from_key(k)
            if addr:
                return addr

    return None


def _fmt_money(x: float) -> str:
    return f"{x:.2f}"


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_datetime(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return dt.datetime.fromisoformat(text)
        except Exception:
            return None
    return None


def _extract_end_time(raw: Any, visited: Optional[Set[int]] = None) -> Optional[dt.datetime]:
    if not isinstance(raw, dict):
        return None
    if visited is None:
        visited = set()
    obj_id = id(raw)
    if obj_id in visited:
        return None
    visited.add(obj_id)

    for key in (
        "conditionEndTime",
        "condition_end_time",
        "endTime",
        "end_time",
        "endDate",
        "end_date",
        "closeDate",
        "close_date",
        "expiry",
        "expiration",
        "expiresAt",
        "marketEndTime",
        "market_end_time",
        "resolveTime",
        "resolve_time",
    ):
        candidate = raw.get(key)
        dt_value = _parse_datetime(candidate)
        if dt_value is not None:
            return dt_value

    for nested_key in ("market", "condition", "event", "marketData", "collection"):
        nested = raw.get(nested_key)
        if isinstance(nested, dict):
            nested_dt = _extract_end_time(nested, visited)
            if nested_dt is not None:
                return nested_dt

    return None


def _format_remaining(end_time: Optional[dt.datetime]) -> str:
    if end_time is None:
        return "-"
    now = _now_utc()
    delta = end_time - now
    total_seconds = int(delta.total_seconds())
    sign = 1 if total_seconds >= 0 else -1
    total_seconds = abs(total_seconds)

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    if sign >= 0:
        if days > 0:
            return f"剩余{days}天{hours}小时"
        if hours > 0:
            return f"剩余{hours}小时{minutes}分钟"
        if minutes > 0:
            return f"剩余{minutes}分钟"
        return "剩余不到1分钟"
    else:
        if days > 0:
            return f"已结束{days}天{hours}小时前"
        if hours > 0:
            return f"已结束{hours}小时{minutes}分钟前"
        if minutes > 0:
            return f"已结束{minutes}分钟前"
        return "刚刚结束"


def _fetch_positions_eoa(user_addr: str) -> List[Dict[str, Any]]:
    # 官方文档：GET /positions?user=<address>
    # 说明“user address (required)”，返回中字段名仍叫 proxyWallet，但值就是传入的地址
    # 参考：docs.polymarket.com (Data-API /positions)
    url = f"{DATA_API_HOST}/positions"
    params = {
        "user": user_addr,
        "sizeThreshold": 0,
        "limit": 500,
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return data


def main(argv: List[str]) -> int:
    as_json = "--json" in argv

    user = _infer_wallet_address()
    if not user:
        print("[ERR] 未能确定 EOA 地址。请设置 POLY_EOA_ADDRESS 或提供私钥（POLY_KEY 等）。", file=sys.stderr)
        return 2

    print(f"[INFO] 使用钱包地址：{user}")

    try:
        positions = _fetch_positions_eoa(user)
    except requests.HTTPError as e:
        print(f"[ERR] Data-API 查询失败：HTTP {e.response.status_code}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"[ERR] Data-API 查询异常：{e}", file=sys.stderr)
        return 3

    if not positions:
        print("[INFO] 当前没有持仓（Data-API 返回空列表）。")
        return 0

    if as_json:
        print(json.dumps(positions, ensure_ascii=False, indent=2))
        return 0

    # 人类可读输出
    print("\n[HOLDINGS] 当前持仓：")
    for i, p in enumerate(positions, 1):
        # 关键字段（容错读取）
        title = p.get("title") or p.get("eventSlug") or p.get("slug") or ""
        outcome = p.get("outcome") or ""
        size = p.get("size") or 0
        avg_price = p.get("avgPrice") or 0
        cur_price = p.get("curPrice") or 0
        cash_pnl = p.get("cashPnl") or 0
        percent_pnl = p.get("percentPnl") or 0
        asset = p.get("asset") or ""  # token_id
        end_time = _extract_end_time(p)
        if end_time is not None and end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=dt.timezone.utc)
        if end_time is not None:
            end_iso = end_time.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        else:
            end_iso = "-"
        remain_text = _format_remaining(end_time)

        print(f"{i:>2}. {title} | {outcome} | token_id={asset}")
        print(f"    数量={size} | 均价={_fmt_money(avg_price)} | 标记价={_fmt_money(cur_price)} | "
              f"P/L={_fmt_money(cash_pnl)} ({percent_pnl:+.2f}%)")
        print(f"    结束时间={end_iso} | {remain_text}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
