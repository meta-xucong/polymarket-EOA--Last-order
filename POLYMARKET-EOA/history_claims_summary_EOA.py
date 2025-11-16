#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Polymarket Claim 统计脚本（EOA 版）。

功能定位：
- 基于 Data-API 的 /trades 与 /activity?type=REDEEM，按「现金流口径」统计：
  - 买入：USDC 流出；卖出：USDC 流入；redeem：USDC 流入（官方 usdcSize）。
- 默认以「redeem 时间」作为筛选入口，列出从指定日期起发生的所有 claim 事件，
  同时给出相关 token 的现金流汇总。

使用示例：
    python3 history_claims_summary_EOA.py
    python3 history_claims_summary_EOA.py --filter-by trade --json
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

try:  # 复用 view_positions_EOA 内的钱包检测与格式化
    from view_positions_EOA import (  # type: ignore
        DATA_API_HOST as _VP_DATA_API_HOST,
        _infer_wallet_address as _vp_infer_wallet_address,
        _fmt_money as _vp_fmt_money,
    )
except Exception:  # pragma: no cover - 回退到最少依赖
    _VP_DATA_API_HOST = os.environ.get("DATA_API_HOST", "https://data-api.polymarket.com").rstrip("/")

    def _vp_infer_wallet_address() -> Optional[str]:  # type: ignore
        from view_positions_EOA import _infer_wallet_address  # type: ignore  # noqa: E401

        return _infer_wallet_address()

    def _vp_fmt_money(value: float) -> str:
        return f"{value:.2f}"

DATA_API_HOST = os.environ.get("DATA_API_HOST", _VP_DATA_API_HOST).rstrip("/")
DEFAULT_SINCE_DATE = "2025-11-13"
UTC_PLUS_8 = timezone(timedelta(hours=8))
DEBUG_LOG = False


@dataclass
class Event:
    asset: str
    condition_id: Optional[str]
    outcome_index: Optional[int]
    outcome: str
    title: str
    slug: str
    type: str  # TRADE_BUY / TRADE_SELL / REDEEM
    size: float
    price: Optional[float]
    usdc_size: Optional[float]
    cash: float
    timestamp: float


def _debug_print(message: str) -> None:
    if DEBUG_LOG:
        print(f"[DEBUG] {message}")


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


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _first_present(entry: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in entry:
            return entry.get(key)
    return None


def _normalize_timestamp(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
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


def _extract_items(payload: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    cursor: Optional[str] = None

    def _extract_cursor(raw: Dict[str, Any]) -> Optional[str]:
        for key in ("next", "nextCursor", "next_page", "nextPage", "cursor"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    if isinstance(payload, list):
        return [it for it in payload if isinstance(it, dict)], None

    if isinstance(payload, dict):
        for key in ("data", "results", "items", "fills", "positions", "history"):
            arr = payload.get(key)
            if isinstance(arr, list):
                cursor = _extract_cursor(payload)
                break
        else:
            arr = []
            cursor = _extract_cursor(payload)
        return [it for it in arr if isinstance(it, dict)], cursor

    return [], None


def _fetch_paginated(url: str, params: Dict[str, Any], limit: int, max_pages: int) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    page = 0
    while page < max_pages:
        page += 1
        req_params = dict(params)
        req_params["limit"] = limit
        if cursor:
            req_params["cursor"] = cursor
        resp = requests.get(url, params=req_params, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
        items, cursor = _extract_items(payload)
        results.extend(items)
        _debug_print(f"GET {url} page={page} count={len(items)} cursor={cursor}")
        if not cursor:
            break
    return results


def _fetch_trades(user: str, limit: int = 500, max_pages: int = 5) -> List[Dict[str, Any]]:
    url = f"{DATA_API_HOST}/trades"
    params = {"user": user}
    return _fetch_paginated(url, params, limit, max_pages)


def _fetch_activity_redeem(user: str, limit: int = 500, max_pages: int = 5) -> List[Dict[str, Any]]:
    url = f"{DATA_API_HOST}/activity"
    params = {"user": user, "type": "REDEEM"}
    return _fetch_paginated(url, params, limit, max_pages)


def _build_trade_event(entry: Dict[str, Any]) -> Optional[Event]:
    side = str(entry.get("side") or entry.get("type") or "").upper()
    if side not in ("BUY", "SELL"):
        return None
    asset = _first_present(entry, ("asset", "tokenId", "token_id", "tokenID"))
    if not asset:
        return None
    condition_id = _first_present(entry, ("conditionId", "condition_id"))
    outcome_index = None
    oi_raw = _first_present(entry, ("outcomeIndex", "outcome_index"))
    if oi_raw is not None:
        try:
            outcome_index = int(float(oi_raw))
        except Exception:
            outcome_index = None
    outcome = str(entry.get("outcome") or entry.get("outcomeName") or entry.get("outcomeLabel") or "").strip()
    title = str(entry.get("title") or entry.get("marketQuestion") or "").strip()
    slug = str(entry.get("slug") or entry.get("marketSlug") or "").strip()
    size = _safe_float(entry.get("size"))
    price = _optional_float(entry.get("price"))
    price = price if price is not None else 0.0
    cash = -size * price if side == "BUY" else size * price
    ts = _entry_timestamp(entry)
    if ts is None:
        return None
    return Event(
        asset=str(asset),
        condition_id=str(condition_id) if condition_id else None,
        outcome_index=outcome_index,
        outcome=outcome,
        title=title,
        slug=slug,
        type=f"TRADE_{side}",
        size=size,
        price=price,
        usdc_size=None,
        cash=cash,
        timestamp=ts,
    )


def _build_redeem_event(entry: Dict[str, Any]) -> Optional[Event]:
    asset = _first_present(entry, ("asset", "tokenId", "token_id", "tokenID"))
    if not asset:
        return None
    condition_id = _first_present(entry, ("conditionId", "condition_id"))
    outcome_index = None
    oi_raw = _first_present(entry, ("outcomeIndex", "outcome_index"))
    if oi_raw is not None:
        try:
            outcome_index = int(float(oi_raw))
        except Exception:
            outcome_index = None
    outcome = str(entry.get("outcome") or entry.get("outcomeLabel") or "").strip()
    title = str(entry.get("title") or "").strip()
    slug = str(entry.get("slug") or entry.get("eventSlug") or "").strip()
    size = _safe_float(entry.get("size"))
    usdc_size = _optional_float(entry.get("usdcSize"))
    cash = _safe_float(usdc_size, 0.0)
    ts = _entry_timestamp(entry)
    if ts is None:
        return None
    return Event(
        asset=str(asset),
        condition_id=str(condition_id) if condition_id else None,
        outcome_index=outcome_index,
        outcome=outcome,
        title=title,
        slug=slug,
        type="REDEEM",
        size=size,
        price=None,
        usdc_size=usdc_size,
        cash=cash,
        timestamp=ts,
    )


def _prompt_since_date(cli_value: Optional[str] = None) -> Tuple[str, float]:
    if cli_value:
        date_text = cli_value.strip()
    else:
        prompt = (
            f"请输入查询起始日期（UTC+8，格式YYYY-MM-DD，默认为 {DEFAULT_SINCE_DATE}）："
        )
        user_input = input(prompt)
        date_text = (user_input or "").strip() or DEFAULT_SINCE_DATE
    try:
        base_date = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        print(f"[WARN] 无法解析日期 '{date_text}'，回退到默认 {DEFAULT_SINCE_DATE}。")
        date_text = DEFAULT_SINCE_DATE
        base_date = datetime.strptime(date_text, "%Y-%m-%d")
    aware_dt = base_date.replace(tzinfo=UTC_PLUS_8)
    since_ts = aware_dt.astimezone(timezone.utc).timestamp()
    return date_text, since_ts


def _collect_events(trades: List[Dict[str, Any]], redeem_entries: List[Dict[str, Any]]) -> List[Event]:
    events: List[Event] = []
    for entry in trades:
        evt = _build_trade_event(entry)
        if evt:
            events.append(evt)
    for entry in redeem_entries:
        evt = _build_redeem_event(entry)
        if evt:
            events.append(evt)
    events.sort(key=lambda e: e.timestamp)
    return events


@dataclass
class PositionSummary:
    asset: str
    condition_id: Optional[str]
    outcome_index: Optional[int]
    title: str
    outcome: str
    slug: str
    buy_size_total: float = 0.0
    buy_cost_total: float = 0.0
    sell_size_total: float = 0.0
    sell_proceeds_total: float = 0.0
    redeem_size_total: float = 0.0
    redeem_usdc_total: float = 0.0
    cash_flow_total: float = 0.0
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None

    @property
    def avg_buy_price(self) -> float:
        return (self.buy_cost_total / self.buy_size_total) if self.buy_size_total > 0 else 0.0

    def apply_event(self, event: Event) -> None:
        self.title = self.title or event.title
        self.outcome = self.outcome or event.outcome
        self.slug = self.slug or event.slug
        if event.type == "TRADE_BUY":
            self.buy_size_total += event.size
            self.buy_cost_total += event.size * (event.price or 0.0)
        elif event.type == "TRADE_SELL":
            self.sell_size_total += event.size
            self.sell_proceeds_total += event.size * (event.price or 0.0)
        elif event.type == "REDEEM":
            self.redeem_size_total += event.size
            self.redeem_usdc_total += event.usdc_size or 0.0
        self.cash_flow_total += event.cash
        if self.first_ts is None or event.timestamp < self.first_ts:
            self.first_ts = event.timestamp
        if self.last_ts is None or event.timestamp > self.last_ts:
            self.last_ts = event.timestamp


def _aggregate_positions(events: List[Event]) -> Dict[str, PositionSummary]:
    bucket: Dict[str, PositionSummary] = {}
    for evt in events:
        pos = bucket.get(evt.asset)
        if not pos:
            pos = PositionSummary(
                asset=evt.asset,
                condition_id=evt.condition_id,
                outcome_index=evt.outcome_index,
                title=evt.title,
                outcome=evt.outcome,
                slug=evt.slug,
            )
            bucket[evt.asset] = pos
        pos.apply_event(evt)
    return bucket


def _filter_events_by_mode(events: List[Event], since_ts: float, mode: str) -> Tuple[List[Event], List[Event]]:
    redeem_events_since = [e for e in events if e.type == "REDEEM" and e.timestamp >= since_ts]
    if mode == "redeem":
        scope_assets = {e.asset for e in redeem_events_since}
        scoped_events = [e for e in events if e.asset in scope_assets]
    else:  # trade 口径，直接按时间过滤所有事件
        scoped_events = [e for e in events if e.timestamp >= since_ts]
    return redeem_events_since, scoped_events


def _print_claim_events(events: List[Event]) -> None:
    if not events:
        print("[INFO] 在指定时间后没有新的 claim 事件。")
        return
    print("\n[CLAIMS] Redeem 事件明细：")
    events_sorted = sorted(events, key=lambda e: e.timestamp)
    total_usdc = sum(e.usdc_size or 0.0 for e in events_sorted)
    for idx, evt in enumerate(events_sorted, 1):
        ts_text = _fmt_timestamp_local(evt.timestamp)
        print(
            f"{idx:>3}. {evt.title or '-'} | {evt.outcome or '-'} | token_id={evt.asset}"
        )
        print(
            "     "
            f"赎回份数={evt.size:.4f} | 赎回金额≈{_vp_fmt_money(evt.usdc_size or 0.0)} | 时间={ts_text}"
        )
        if evt.slug:
            print(f"     slug={evt.slug}")
    print(f"\n[CLAIMS] 统计：共 {len(events_sorted)} 笔，赎回总额≈{_vp_fmt_money(total_usdc)} USDC")


def _print_positions(positions: Dict[str, PositionSummary]) -> None:
    if not positions:
        print("[INFO] 没有需要展示的仓位汇总。")
        return
    print("\n[POSITIONS] 按 token 汇总：")
    total_cash = 0.0
    for idx, (asset, pos) in enumerate(sorted(positions.items(), key=lambda kv: kv[1].last_ts or 0, reverse=True), 1):
        total_cash += pos.cash_flow_total
        print(f"{idx:>3}. {pos.title or '-'} | {pos.outcome or '-'} | token_id={asset}")
        print(
            "     "
            f"买入总量={pos.buy_size_total:.4f} | 买入均价≈{_vp_fmt_money(pos.avg_buy_price)} | 买入成本≈{_vp_fmt_money(pos.buy_cost_total)}"
        )
        print(
            "     "
            f"卖出总量={pos.sell_size_total:.4f} | 卖出收入≈{_vp_fmt_money(pos.sell_proceeds_total)}"
        )
        print(
            "     "
            f"redeem 份数={pos.redeem_size_total:.4f} | redeem 收入≈{_vp_fmt_money(pos.redeem_usdc_total)}"
        )
        print(
            "     "
            f"现金流合计≈{_vp_fmt_money(pos.cash_flow_total)} | 时间区间：{_fmt_timestamp_local(pos.first_ts)} -> {_fmt_timestamp_local(pos.last_ts)}"
        )
        print()
    print(f"[POSITIONS] 现金流汇总：≈{_vp_fmt_money(total_cash)} USDC")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Polymarket Claim 统计工具（现金流口径）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument(
        "--filter-by",
        choices=["redeem", "trade"],
        default="redeem",
        help="统计入口：redeem=按赎回时间筛选 token（默认），trade=按事件时间直接过滤",
    )
    parser.add_argument("--since-date", dest="since_date", help="筛选起始日期，格式 YYYY-MM-DD，默认交互式输入")
    parser.add_argument("--trades-limit", type=int, default=500, help="/trades 每页条数")
    parser.add_argument("--trades-pages", type=int, default=5, help="/trades 最大翻页数")
    parser.add_argument("--activity-limit", type=int, default=500, help="/activity 每页条数")
    parser.add_argument("--activity-pages", type=int, default=5, help="/activity 最大翻页数")
    args = parser.parse_args(argv)

    global DEBUG_LOG
    DEBUG_LOG = bool(args.debug)

    user = _vp_infer_wallet_address()
    if not user:
        print("[ERR] 未能确定 EOA 地址，请检查 POLY_EOA_ADDRESS 或相关配置。", file=sys.stderr)
        return 2
    print(f"[INFO] 使用钱包地址：{user}")

    since_date_text, since_ts = _prompt_since_date(args.since_date)

    try:
        trades = _fetch_trades(
            user,
            limit=max(1, args.trades_limit),
            max_pages=max(1, args.trades_pages),
        )
    except Exception as exc:
        print(f"[ERR] 获取 /trades 失败：{exc}", file=sys.stderr)
        return 3

    try:
        redeem_entries = _fetch_activity_redeem(
            user,
            limit=max(1, args.activity_limit),
            max_pages=max(1, args.activity_pages),
        )
    except Exception as exc:
        print(f"[ERR] 获取 /activity?type=REDEEM 失败：{exc}", file=sys.stderr)
        return 4

    _debug_print(f"/trades total={len(trades)} | /activity(REDEEM) total={len(redeem_entries)}")

    events = _collect_events(trades, redeem_entries)
    redeem_events_since, scoped_events = _filter_events_by_mode(events, since_ts, args.filter_by)

    positions = _aggregate_positions(scoped_events)

    if args.json:
        output = {
            "wallet": user,
            "since_date_utc8": since_date_text,
            "filter_by": args.filter_by,
            "claim_events": [evt.__dict__ for evt in redeem_events_since],
            "positions": {k: v.__dict__ for k, v in positions.items()},
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    _print_claim_events(redeem_events_since)
    _print_positions(positions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
