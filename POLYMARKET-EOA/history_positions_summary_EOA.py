
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


def _fetch_trades(user: str, limit: int, max_pages: int) -> List[Dict[str, Any]]:
    """拉取用户的历史成交数据，分页获取"""
    # Now using the correct DATA_API_HOST for /trades endpoint
    trades_endpoint = f"{DATA_API_HOST}/trades"
    params = {"user": user, "limit": limit}
    return _paginate(trades_endpoint, params, max_pages)


def _fetch_activity(user: str, limit: int, max_pages: int) -> List[Dict[str, Any]]:
    endpoint = f"{DATA_API_HOST}/activity"
    params = {"user": user, "limit": limit}
    return _paginate(endpoint, params, max_pages)


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


def _extract_asset_id(entry: Dict[str, Any]) -> str:
    for key in ("asset", "tokenId", "token_id", "tokenID", "id"):
        value = entry.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    token = entry.get("token")
    if isinstance(token, dict):
        for key in ("tokenId", "id", "asset"):
            value = token.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
    return ""


def _extract_cash_pnl(entry: Dict[str, Any]) -> Optional[float]:
    for key in (
        "cashPnl",
        "cashPnlTotal",
        "realizedPnl",
        "realizedPnL",
        "pnl",
        "PnL",
        "profit",
        "payout",
    ):
        if key in entry:
            val = _safe_float(entry.get(key))
            return val
    return None


@dataclass
class BuyPosition:
    asset: str
    title: str = ""
    outcome: str = ""
    market_slug: str = ""
    condition_id: str = ""
    icon: str = ""
    total_size: float = 0.0
    total_cost: float = 0.0
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None

    def register_trade(self, trade: Dict[str, Any], size: float, price: float, ts: Optional[float]) -> None:
        if size <= 0:
            return
        self.total_size += size
        self.total_cost += size * price
        if not self.title:
            self.title = (
                trade.get("title")
                or trade.get("market")
                or trade.get("eventSlug")
                or trade.get("slug")
                or ""
            )
        if not self.outcome:
            self.outcome = trade.get("outcome") or trade.get("outcomeName") or ""
        if not self.market_slug:
            self.market_slug = trade.get("slug") or trade.get("marketSlug") or trade.get("eventSlug") or ""
        if not self.condition_id:
            self.condition_id = trade.get("conditionId") or trade.get("condition_id") or ""
        if not self.icon:
            self.icon = trade.get("icon") or ""
        if ts is not None:
            if self.first_ts is None or ts < self.first_ts:
                self.first_ts = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts

    @property
    def avg_price(self) -> float:
        if self.total_size <= 0:
            return 0.0
        return self.total_cost / self.total_size


def _summarize_buy_trades(trades: Iterable[Dict[str, Any]]) -> Dict[str, BuyPosition]:
    summary: Dict[str, BuyPosition] = {}
    for trade in trades:
        side = (trade.get("side") or "").upper()
        if side != "BUY":
            continue
        asset = _extract_asset_id(trade)
        if not asset:
            continue
        size = _safe_float(trade.get("size"))
        price = _safe_float(trade.get("price"))
        if size <= 0:
            continue
        ts = _entry_timestamp(trade)
        bucket = summary.get(asset)
        if bucket is None:
            bucket = BuyPosition(asset=asset)
            summary[asset] = bucket
        bucket.register_trade(trade, size, price, ts)
    return summary


def _summarize_activity(entries: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    resolved: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        asset = _extract_asset_id(entry)
        if not asset:
            continue
        ts = _entry_timestamp(entry)
        info = {
            "asset": asset,
            "status": entry.get("status") or entry.get("type") or entry.get("action") or "",
            "resolved_outcome": entry.get("winningOutcome")
            or entry.get("resolvedOutcome")
            or entry.get("outcome")
            or entry.get("result"),
            "cash_pnl": _extract_cash_pnl(entry),
            "claim_amount": _safe_float(
                entry.get("claimAmount")
                or entry.get("amountClaimed")
                or entry.get("claimedAmount")
            ),
            "settlement_price": _safe_float(
                entry.get("settlementPrice")
                or entry.get("settledPrice")
                or entry.get("resolvePrice")
            ),
            "timestamp": ts,
            "was_claimed": bool(
                entry.get("claimed")
                or entry.get("isClaimed")
                or entry.get("wasClaimed")
                or (entry.get("type") or "").lower() == "claim"
                or (entry.get("action") or "").lower() == "claim"
            ),
            "raw": entry,
        }
        info["is_resolved"] = bool(
            entry.get("resolved")
            or entry.get("isResolved")
            or entry.get("status") == "resolved"
            or (entry.get("type") or "").lower() in ("claim", "resolve")
        )
        prev = resolved.get(asset)
        prev_ts = prev.get("timestamp") if prev else None
        if prev is None or (ts is not None and (prev_ts is None or ts >= prev_ts)):
            resolved[asset] = info
    return resolved


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
    try:
        base_date = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        print(f"[WARN] 无法解析日期 '{date_text}'，回退到默认 {DEFAULT_SINCE_DATE}。")
        date_text = DEFAULT_SINCE_DATE
        base_date = datetime.strptime(date_text, "%Y-%m-%d")
    aware_dt = base_date.replace(tzinfo=UTC_PLUS_8)
    since_ts = aware_dt.astimezone(timezone.utc).timestamp()  # 强制转换为秒（UTC）
    return date_text, since_ts


def _compose_position_rows(
    positions: Dict[str, BuyPosition],
    realized: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for asset, bucket in positions.items():
        realized_entry = realized.get(asset)
        resolved_ts = realized_entry.get("timestamp") if realized_entry else None
        rows.append(
            {
                "asset": asset,
                "title": bucket.title,
                "outcome": bucket.outcome,
                "marketSlug": bucket.market_slug,
                "conditionId": bucket.condition_id,
                "icon": bucket.icon,
                "totalSize": bucket.total_size,
                "avgEntryPrice": bucket.avg_price,
                "totalCost": bucket.total_cost,
                "firstBuyTime": bucket.first_ts,
                "lastBuyTime": bucket.last_ts,
                "resolutionTime": resolved_ts,
                "resolutionStatus": realized_entry.get("status") if realized_entry else None,
                "resolvedOutcome": realized_entry.get("resolved_outcome") if realized_entry else None,
                "realizedPnl": realized_entry.get("cash_pnl") if realized_entry else None,
                "claimAmount": realized_entry.get("claim_amount") if realized_entry else None,
                "settlementPrice": realized_entry.get("settlement_price") if realized_entry else None,
                "isResolved": realized_entry.get("is_resolved") if realized_entry else False,
                "wasClaimed": realized_entry.get("was_claimed") if realized_entry else False,
            }
        )
    rows.sort(key=lambda r: (r.get("lastBuyTime") or 0), reverse=True)
    return rows


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

    try:
        history_entries = _fetch_activity(
            user,
            limit=max(1, args.history_limit),
            max_pages=max(1, args.history_pages),
        )
    except Exception as exc:
        print(f"[WARN] 获取历史仓位 /activity 失败：{exc}")
        history_entries = []

    filtered_trades = _filter_entries_since(trades, since_ts)
    filtered_history = _filter_entries_since(history_entries, since_ts)

    buy_positions = _summarize_buy_trades(filtered_trades)
    realized_map = _summarize_activity(filtered_history)
    rows = _compose_position_rows(buy_positions, realized_map)

    if args.json:
        output = {
            "wallet": user,
            "since_date_utc8": since_date_text,
            "positions": rows,
            "trades": filtered_trades,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print("[INFO] 在指定区间内没有买入记录。")
        return 0

    print("\n[HISTORY] 历史买入持仓（含结算盈亏）：")
    for idx, row in enumerate(rows, 1):
        total_size = row.get("totalSize") or 0.0
        avg_price = row.get("avgEntryPrice") or 0.0
        total_cost = row.get("totalCost") or 0.0
        realized_pnl = row.get("realizedPnl")
        claim_amount = row.get("claimAmount")
        resolution_status = row.get("resolutionStatus")
        if not resolution_status:
            resolution_status = "已结算" if row.get("isResolved") else "未结算"
        resolved_outcome = row.get("resolvedOutcome") or "-"
        first_ts = _fmt_timestamp_local(row.get("firstBuyTime"))
        last_ts = _fmt_timestamp_local(row.get("lastBuyTime"))
        resolution_time = _fmt_timestamp_local(row.get("resolutionTime"))
        realized_text = (
            _vp_fmt_money(realized_pnl) if isinstance(realized_pnl, (int, float)) else "-"
        )
        claim_text = (
            _vp_fmt_money(claim_amount) if isinstance(claim_amount, (int, float)) else "-"
        )
        print(
            f"{idx:>2}. {row.get('title') or '-'} | {row.get('outcome') or '-'} | token_id={row.get('asset')}"
        )
        print(
            "    "
            f"买入方向=BUY | 买入总量={total_size:.4f} | 均价={_vp_fmt_money(avg_price)} | 总成本≈{_vp_fmt_money(total_cost)}"
        )
        print(f"    买入时间区间：{first_ts} -> {last_ts}")
        print(
            "    "
            f"结算状态={resolution_status} | 结算结果={resolved_outcome} | 已实现盈亏={realized_text} | 结算时间={resolution_time}"
        )
        print(f"    领取金额/赔付={claim_text}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
