#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Polymarket 历史成交与仓位盈亏统计脚本（EOA 优先版）。

功能概览：
1. 自动识别当前钱包地址（沿用 ``view_positions_EOA.py`` 的逻辑）。
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

    if isinstance(payload, list):
        return [it for it in payload if isinstance(it, dict)], None

    if isinstance(payload, dict):
        for key in ("data", "results", "items", "fills", "positions", "history"):
            arr = payload.get(key)
            if isinstance(arr, list):
                break
        else:
            arr = []

        next_cursor = payload.get("next") or payload.get("nextCursor")
        if not next_cursor:
            meta = payload.get("meta")
            if isinstance(meta, dict):
                next_cursor = meta.get("next") or meta.get("nextCursor")
        return [it for it in arr if isinstance(it, dict)], next_cursor

    return [], None


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
    try:
        user_input = input(prompt)
    except EOFError:
        user_input = ""
    date_text = (user_input or "").strip() or DEFAULT_SINCE_DATE
    try:
        base_date = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        print(
            f"[WARN] 日期格式无效（{date_text}），已使用默认值 {DEFAULT_SINCE_DATE}。"
        )
        base_date = datetime.strptime(DEFAULT_SINCE_DATE, "%Y-%m-%d")
        date_text = DEFAULT_SINCE_DATE
    aware_dt = base_date.replace(tzinfo=UTC_PLUS_8)
    since_ts = aware_dt.astimezone(timezone.utc).timestamp()
    return date_text, since_ts
def _activity_endpoints() -> List[str]:
    endpoints: List[str] = []
    for host in (DATA_API_HOST, GAMMA_API_HOST, GAMMA_ALT_HOST):
        if not host:
            continue
        base = host.rstrip("/")
        endpoint = f"{base}/activity"
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    return endpoints


def _fetch_trades(user: str, limit: int, max_pages: int) -> List[Dict[str, Any]]:
    capped_limit = max(1, min(limit, 10000))
    trade_params = {"user": user, "takerOnly": False}
    alias_params = {
        "limit": capped_limit,
        "user": user,
        "wallet": user,
        "walletAddress": user,
        "address": user,
    }
    last_error: Optional[Exception] = None
    for endpoint in _trade_history_endpoints():
        try:
            if endpoint.endswith("/trades") or endpoint.endswith("/trades/history") or endpoint.endswith("/trades-history"):
                return _paginate_offset(endpoint, trade_params, capped_limit, max_pages)
            return _paginate(endpoint, alias_params, max_pages)
        except requests.HTTPError as exc:  # pragma: no cover - 网络异常仅运行时触发
            last_error = exc
            status = getattr(exc.response, "status_code", None)
            if status in {404, 405}:
                continue
            raise
        except Exception as exc:  # pragma: no cover
            last_error = exc
            continue
    if last_error:
        raise last_error
    return []


def _fetch_positions_history(user: str, limit: int, max_pages: int) -> List[Dict[str, Any]]:
    capped_limit = max(1, min(limit, 1000))
    params = {"user": user}
    alias_params = {
        "user": user,
        "wallet": user,
        "walletAddress": user,
        "address": user,
        "limit": capped_limit,
    }
    endpoints: List[Tuple[str, bool]] = []
    for endpoint in _activity_endpoints():
        endpoints.append((endpoint, True))
    for host in (DATA_API_HOST, GAMMA_API_HOST, GAMMA_ALT_HOST):
        if not host:
            continue
        base = host.rstrip("/")
        endpoints.append((f"{base}/positions/history", False))
        endpoints.append((f"{base}/positions-history", False))
    last_error: Optional[Exception] = None
    for url, is_activity in endpoints:
        try:
            if is_activity:
                return _paginate_offset(url, params, capped_limit, max_pages)
            return _paginate(url, alias_params, max_pages)
        except requests.HTTPError as exc:  # pragma: no cover - 仅在 API 不支持时出现
            last_error = exc
            if exc.response.status_code in {404, 405}:
                continue
            raise
        except Exception as exc:  # pragma: no cover
            last_error = exc
            continue
    if last_error:
        raise last_error
    return []


@dataclass
class TradeSummary:
    trades_count: int
    buy_count: int
    buy_volume: float
    buy_notional: float
    sell_count: int
    sell_volume: float
    sell_notional: float

    @property
    def avg_buy_price(self) -> float:
        if self.buy_volume <= 0:
            return 0.0
        return self.buy_notional / self.buy_volume

    @property
    def avg_sell_price(self) -> float:
        if self.sell_volume <= 0:
            return 0.0
        return self.sell_notional / self.sell_volume


def _summarize_trades(fills: Iterable[Dict[str, Any]]) -> TradeSummary:
    buy_volume = buy_notional = sell_volume = sell_notional = 0.0
    total = buy_count = sell_count = 0
    for fill in fills:
        side = str(fill.get("side") or fill.get("direction") or "").lower()
        size = _safe_float(fill.get("size") or fill.get("amount") or fill.get("quantity"))
        price = _safe_float(fill.get("price") or fill.get("avgPrice") or fill.get("fillPrice"))
        if size <= 0 or price <= 0:
            continue
        total += 1
        if side == "buy":
            buy_count += 1
            buy_volume += size
            buy_notional += size * price
        elif side == "sell":
            sell_count += 1
            sell_volume += size
            sell_notional += size * price
    return TradeSummary(
        trades_count=total,
        buy_count=buy_count,
        buy_volume=buy_volume,
        buy_notional=buy_notional,
        sell_count=sell_count,
        sell_volume=sell_volume,
        sell_notional=sell_notional,
    )


@dataclass
class HistorySummary:
    positions_count: int
    realized_cash_pnl: float
    avg_percent_pnl: Optional[float]
    zero_markets: List[Dict[str, Any]]
    entries: List[Dict[str, Any]]


def _extract_first_float(entry: Dict[str, Any], fields: Tuple[str, ...]) -> Optional[float]:
    for key in fields:
        if key not in entry:
            continue
        value = entry.get(key)
        if value in (None, ""):
            continue
        return _safe_float(value)
    return None


def _extract_side(entry: Dict[str, Any]) -> Optional[str]:
    for key in (
        "side",
        "direction",
        "action",
        "activityType",
        "type",
        "status",
    ):
        raw = entry.get(key)
        if raw in (None, ""):
            continue
        text = str(raw).strip().lower()
        if text in {"buy", "sell"}:
            return text
    return None


def _summarize_history(entries: Iterable[Dict[str, Any]]) -> HistorySummary:
    realized = 0.0
    pct_values: List[float] = []
    zero_markets: List[Dict[str, Any]] = []
    details: List[Dict[str, Any]] = []
    count = 0
    for entry in entries:
        status_text = str(
            entry.get("status")
            or entry.get("state")
            or entry.get("type")
            or entry.get("activityType")
            or entry.get("action")
            or ""
        ).lower()
        has_history_fields = any(
            key in entry
            for key in (
                "cashPnl",
                "realizedPnl",
                "percentPnl",
                "percentReturn",
                "payout",
                "payoutAmount",
                "claimTx",
            )
        )
        claim_like = status_text in {
            "claim",
            "claimed",
            "redeem",
            "redeemed",
            "resolution",
            "resolved",
            "close",
            "closed",
        }
        if not (claim_like or has_history_fields):
            continue

        count += 1
        realized_component = _safe_float(
            entry.get("cashPnl")
            or entry.get("realizedPnl")
            or entry.get("payout")
            or entry.get("payoutAmount")
            or entry.get("amount")
            or entry.get("value")
        )
        realized += realized_component
        percent = (
            entry.get("percentPnl")
            or entry.get("percent_pnl")
            or entry.get("percentReturn")
        )
        pct_val = _safe_float(percent, default=0.0)
        has_pct = percent not in (None, "", [], {})
        if has_pct:
            pct_values.append(pct_val)

        buy_price = _extract_first_float(
            entry,
            (
                "avgPrice",
                "averagePrice",
                "entryPrice",
                "buyPrice",
                "price",
                "fillPrice",
                "executionPrice",
                "tradePrice",
            ),
        )
        settlement_price = _extract_first_float(
            entry,
            (
                "settlementPrice",
                "resolvedPrice",
                "payoutPerToken",
                "tokenPayout",
                "perSharePayout",
                "finalPrice",
                "curPrice",
                "markPrice",
            ),
        )
        size_now = entry.get("size") or entry.get("quantity") or entry.get("tokens")
        size_now_val = None
        if size_now not in (None, ""):
            size_now_val = _safe_float(size_now)

        zero_reason = None
        if settlement_price is not None and settlement_price <= 0.0001:
            zero_reason = "price"
        elif size_now_val is not None and size_now_val <= 0:
            zero_reason = "size"

        side = _extract_side(entry)
        ts = _entry_timestamp(entry)
        detail = {
            "title": entry.get("title")
            or entry.get("market")
            or entry.get("question")
            or entry.get("slug"),
            "outcome": entry.get("outcome"),
            "side": side,
            "buy_price": buy_price,
            "settlement_price": settlement_price,
            "realized_cash_pnl": realized_component,
            "percent_pnl": pct_val if has_pct else None,
            "size": size_now_val,
            "timestamp": ts,
            "timestamp_local": _fmt_timestamp_local(ts),
            "claim_tx": entry.get("claimTx") or entry.get("transactionHash"),
        }
        details.append(detail)

        if zero_reason:
            zero_markets.append(
                {
                    "title": detail["title"],
                    "outcome": detail["outcome"],
                    "cashPnl": detail["realized_cash_pnl"],
                    "percentPnl": detail["percent_pnl"],
                    "claimTx": detail["claim_tx"],
                }
            )

    avg_percent = None
    if pct_values:
        avg_percent = sum(pct_values) / len(pct_values)

    return HistorySummary(
        positions_count=count,
        realized_cash_pnl=realized,
        avg_percent_pnl=avg_percent,
        zero_markets=zero_markets,
        entries=details,
    )


def _fmt_money(value: float) -> str:
    return _vp_fmt_money(value)


def _fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:.4f}"


def _print_zero_markets(zero_markets: List[Dict[str, Any]]) -> None:
    if not zero_markets:
        print("[CLAIMS] 没有侦测到价格归零的 claim 记录。")
        return
    print("[CLAIMS] Claim 后价格归零的市场：")
    for idx, info in enumerate(zero_markets, 1):
        title = info.get("title") or "(未知市场)"
        outcome = info.get("outcome") or "?"
        pnl = _fmt_money(_safe_float(info.get("cashPnl")))
        pct = info.get("percentPnl")
        if pct is None:
            pct_txt = "N/A"
        else:
            pct_txt = f"{pct:+.2f}%"
        claim_tx = info.get("claimTx") or "-"
        print(f"  {idx:>2}. {title} | {outcome} | P/L={pnl} ({pct_txt}) | claimTx={claim_tx}")


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

    user = _vp_infer_wallet_address()
    if not user:
        print("[ERR] 未能确定 EOA 地址，请设置 POLY_EOA_ADDRESS 或相关环境变量。", file=sys.stderr)
        return 2

    print(f"[INFO] 使用钱包地址：{user}")

    try:
        current_positions = _vp_fetch_positions(user)
    except Exception as exc:
        print(f"[ERR] 获取当前持仓失败：{exc}", file=sys.stderr)
        return 3

    print(f"[INFO] 当前持仓数量：{len(current_positions)}")

    since_date_text, since_ts = _prompt_since_date()
    print(f"[INFO] 仅统计 {since_date_text} (UTC+8) 当日 00:00 之后的记录。")

    try:
        trades = _fetch_trades(
            user,
            limit=max(1, args.trades_limit),
            max_pages=max(1, args.trades_pages),
        )
    except Exception as exc:
        print(f"[ERR] 获取历史成交失败：{exc}", file=sys.stderr)
        return 4

    filtered_trades = _filter_entries_since(trades, since_ts)
    trade_summary = _summarize_trades(filtered_trades)

    try:
        history_entries = _fetch_positions_history(
            user,
            limit=max(1, args.history_limit),
            max_pages=max(1, args.history_pages),
        )
    except Exception as exc:
        print(f"[WARN] 获取历史仓位失败：{exc}")
        history_entries = []

    filtered_history = _filter_entries_since(history_entries, since_ts)
    history_summary = _summarize_history(filtered_history)

    if args.json:
        output = {
            "wallet": user,
            "since_date_utc8": since_date_text,
            "current_positions": current_positions,
            "trades": {
                "count": trade_summary.trades_count,
                "buy_count": trade_summary.buy_count,
                "buy_volume": trade_summary.buy_volume,
                "buy_notional": trade_summary.buy_notional,
                "avg_buy_price": trade_summary.avg_buy_price,
                "sell_count": trade_summary.sell_count,
                "sell_volume": trade_summary.sell_volume,
                "sell_notional": trade_summary.sell_notional,
                "avg_sell_price": trade_summary.avg_sell_price,
            },
            "history": {
                "count": history_summary.positions_count,
                "realized_cash_pnl": history_summary.realized_cash_pnl,
                "avg_percent_pnl": history_summary.avg_percent_pnl,
                "zero_markets": history_summary.zero_markets,
                "entries": history_summary.entries,
            },
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    print("\n[TRADES] 历史成交统计：")
    print(
        "  买入次数/数量/金额："
        f"{trade_summary.buy_count} 笔 | {trade_summary.buy_volume:.4f} | "
        f"${_fmt_money(trade_summary.buy_notional)} (均价 {_fmt_money(trade_summary.avg_buy_price)})"
    )
    print(
        "  卖出数量/金额："
        f"{trade_summary.sell_count} 笔 | {trade_summary.sell_volume:.4f} | "
        f"${_fmt_money(trade_summary.sell_notional)} (均价 {_fmt_money(trade_summary.avg_sell_price)})"
    )

    print("\n[PNL] 历史仓位统计：")
    print(
        f"  历史仓位数：{history_summary.positions_count}"
        f" | 累计已实现 P/L：${_fmt_money(history_summary.realized_cash_pnl)}"
    )
    if history_summary.avg_percent_pnl is not None:
        print(f"  平均百分比收益：{history_summary.avg_percent_pnl:+.2f}%")
    if history_summary.entries:
        print("\n[HISTORY DETAILS] 历史仓位条目：")
        for idx, detail in enumerate(history_summary.entries, 1):
            title = detail.get("title") or "(未知市场)"
            outcome = detail.get("outcome") or "?"
            ts_txt = detail.get("timestamp_local") or _fmt_timestamp_local(
                detail.get("timestamp")
            )
            side = detail.get("side")
            side_txt = side.upper() if isinstance(side, str) and side else "-"
            buy_txt = _fmt_price(detail.get("buy_price"))
            settle_txt = _fmt_price(detail.get("settlement_price"))
            pnl_txt = _fmt_money(_safe_float(detail.get("realized_cash_pnl")))
            print(
                "  "
                + f"{idx:02d}. [{ts_txt}] {title} ({outcome}) | 方向: {side_txt}"
                + f" | 买入价: {buy_txt} | 结算价: {settle_txt} | 已实现P/L: ${pnl_txt}"
            )
    _print_zero_markets(history_summary.zero_markets)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
