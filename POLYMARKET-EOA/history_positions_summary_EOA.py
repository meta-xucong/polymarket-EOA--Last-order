
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Polymarket 历史成交与仓位盈亏统计脚本（EOA 优先版）。

功能概览：
1. 自动识别当前钱包地址（沿用 ``view_positions_EOA`` 的逻辑）。
2. 拉取 Data-API ``/positions`` 查看当前持仓。
3. 统计 Data-API ``/trades`` 返回的历史成交（买入数量 / 金额 / 均价等）。
4. 使用文档支持的 ``/activity`` 端点（兼容旧 ``/positions/history``）汇总
   历史仓位已实现 PnL，并列出“已 claim 且价格归零”的市场以便复核，
   避免访问未公开的 API。

使用示例：
    python3 history_positions_summary_EOA.py
    python3 history_positions_summary_EOA.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


try:  # 直接复用 view_positions_EOA 内的配置 / 工具
    from view_positions_EOA import (  # type: ignore
        DATA_API_HOST as _VP_DATA_API_HOST,
        _infer_wallet_address as _vp_infer_wallet_address,
        _fetch_positions_eoa as _vp_fetch_positions,
        _fmt_money as _vp_fmt_money,
    )
except Exception:  # pragma: no cover - 回退到最少依赖
    _VP_DATA_API_HOST = os.environ.get(
        "DATA_API_HOST", "https://data-api.polymarket.com"
    ).rstrip("/")

    def _vp_infer_wallet_address() -> Optional[str]:  # type: ignore
        from view_positions_EOA import _infer_wallet_address  # type: ignore  # noqa

        return _infer_wallet_address()

    def _vp_fetch_positions(user: str) -> List[Dict[str, Any]]:  # type: ignore
        from view_positions_EOA import _fetch_positions_eoa  # type: ignore  # noqa

        return _fetch_positions_eoa(user)

    def _vp_fmt_money(value: float) -> str:
        return f"{value:.2f}"


DATA_API_HOST = os.environ.get("DATA_API_HOST", _VP_DATA_API_HOST).rstrip("/")
GAMMA_API_HOST = os.environ.get("GAMMA_API_HOST", "https://gamma-api.polymarket.com").rstrip("/")
GAMMA_ALT_HOST = os.environ.get("GAMMA_ALT_HOST", "https://gamma.polymarket.com").rstrip("/")

DEFAULT_SINCE_DATE = "2025-11-13"
UTC_PLUS_8 = timezone(timedelta(hours=8))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        if not text:
            return default
        return float(text)
    except Exception:
        return default


def _extract_items(payload: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """容错解析 Data-API 返回的 list + cursor。"""
    print(f"[DEBUG] API 返回数据：{payload}")  # 打印返回的原始数据
    if isinstance(payload, list):
        return [it for it in payload if isinstance(it, dict)], None
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "fills", "positions", "history"):
            arr = payload.get(key)
            if isinstance(arr, list):
                break
        else:
            arr = []
        return [it for it in arr if isinstance(it, dict)], None
    return [], None


def _fetch_trades(user: str, limit: int, max_pages: int) -> List[Dict[str, Any]]:
    """拉取用户的历史成交数据，分页获取"""
    # Now using the correct DATA_API_HOST for /trades endpoint
    trades_endpoint = f"{DATA_API_HOST}/trades"
    params = {"user": user, "limit": limit}
    return _paginate(trades_endpoint, params, max_pages)


def _paginate(endpoint: str, params: Dict[str, Any], max_pages: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    for _ in range(max_pages):
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        resp = requests.get(endpoint, params=q, timeout=20)
        resp.raise_for_status()
        items, cursor = _extract_items(resp.json())
        if not items:
            break
        out.extend(items)
        if not cursor:
            break
    return out


def _paginate_offset(
    endpoint: str, params: Dict[str, Any], limit: int, max_pages: int
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    offset = 0
    for _ in range(max_pages):
        q = dict(params)
        q["limit"] = limit
        if offset:
            q["offset"] = offset
        resp = requests.get(endpoint, params=q, timeout=20)
        resp.raise_for_status()
        items, _ = _extract_items(resp.json())
        if not items:
            break
        out.extend(items)
        if len(items) < limit:
            break
        offset += limit
    return out


def _trade_history_endpoints() -> List[str]:
    hosts = [DATA_API_HOST, GAMMA_API_HOST, GAMMA_ALT_HOST]
    paths = [
        "/trades",
        "/trades/history",
        "/trades-history",
        "/fills",
        "/fills/history",
        "/fills-history",
    ]
    endpoints: List[str] = []
    for host in hosts:
        if not host:
            continue
        base = host.rstrip("/")
        for path in paths:
            endpoint = f"{base}{path}"
            if endpoint not in endpoints:
                endpoints.append(endpoint)
    return endpoints


def _normalize_timestamp(value: Any) -> Optional[float]:
    """尽量把 Data-API 返回的各种时间格式转为 UTC 秒数。"""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # 毫秒时间戳
            ts /= 1000.0
        return ts
    text = str(value).strip()
    if not text:
        return None
    try:
        ts = float(text)
        if ts > 1e12:
            ts /= 1000.0
        return ts
    except Exception:
        pass

    iso_text = text
    if iso_text.endswith("Z"):
        iso_text = iso_text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _entry_timestamp(entry: Dict[str, Any]) -> Optional[float]:
    timestamp_fields = (
        "timestamp",
        "createdAt",
        "updatedAt",
        "time",
        "blockTime",
        "blockTimestamp",
        "settledAt",
        "resolvedAt",
        "closedAt",
    )
    for key in timestamp_fields:
        if key in entry:
            ts = _normalize_timestamp(entry.get(key))
            if ts is not None:
                return ts
    return None


def _fmt_timestamp_local(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(UTC_PLUS_8)
    except Exception:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M")


def _filter_entries_since(entries: Iterable[Dict[str, Any]], since_ts: float) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    for entry in entries:
        ts = _entry_timestamp(entry)
        if ts is None or ts >= since_ts:
            filtered.append(entry)
    return filtered


def _prompt_since_date() -> Tuple[str, float]:
    prompt = (
        f"请输入查询起始日期（UTC+8，格式YYYY-MM-DD，默认为 {DEFAULT_SINCE_DATE}）："
    )
    user_input = input(prompt)
    date_text = (user_input or "").strip() or DEFAULT_SINCE_DATE
    base_date = datetime.strptime(date_text, "%Y-%m-%d")
    aware_dt = base_date.replace(tzinfo=UTC_PLUS_8)
    since_ts = aware_dt.astimezone(timezone.utc).timestamp()  # 强制转换为秒（UTC）
    return date_text, since_ts


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Polymarket 历史仓位统计工具")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument(
        "--trades-limit",
        "--fills-limit",
        dest="trades_limit",
        type=int,
        default=500,
        help="每页拉取的 trades 条数（Data-API /trades，默认500）",
    )
    parser.add_argument(
        "--trades-pages",
        "--fills-pages",
        dest="trades_pages",
        type=int,
        default=5,
        help="trades 最大翻页次数",
    )
    parser.add_argument(
        "--history-limit",
        type=int,
        default=500,
        help="历史仓位每页条数（Data-API /activity，默认500）",
    )
    parser.add_argument(
        "--history-pages",
        type=int,
        default=5,
        help="历史仓位最大翻页次数",
    )
    args = parser.parse_args(argv)

    # Fetch user address
    user = _vp_infer_wallet_address()
    if not user:
        print("[ERR] 未能确定 EOA 地址，请设置 POLY_EOA_ADDRESS 或相关环境变量。", file=sys.stderr)
        return 2

    print(f"[INFO] 使用钱包地址：{user}")

    # Get the since_ts value from the user
    since_date_text, since_ts = _prompt_since_date()

    try:
        trades = _fetch_trades(
            user,
            limit=max(1, args.trades_limit),
            max_pages=max(1, args.trades_pages),
        )
    except Exception as exc:
        print(f"[ERR] 获取历史成交失败：{exc}", file=sys.stderr)
        return 4

    # Filter trades since the specified date
    filtered_trades = _filter_entries_since(trades, since_ts)

    if args.json:
        # Output the filtered trades in JSON format
        output = {
            "wallet": user,
            "since_date_utc8": since_date_text,
            "trades": filtered_trades,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    # Output formatted trade summary
    print("[TRADES] 历史成交统计：")
    for trade in filtered_trades:
        print(
            f"交易方向: {trade['side']} | 资产: {trade['asset']} | "
            f"买入价格: {trade['price']} | 时间戳: {trade['timestamp']} | 市场结算结果: {trade['outcome']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
