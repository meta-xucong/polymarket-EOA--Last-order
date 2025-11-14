#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Polymarket 历史成交与仓位盈亏统计脚本（EOA 优先版）。

功能概览：
1. 自动识别当前钱包地址（沿用 ``view_positions_EOA.py`` 的逻辑）。
2. 拉取 Data-API ``/positions`` 查看当前持仓。
3. 统计 Data-API ``/trades`` 返回的历史成交（买入数量 / 金额 / 均价等）。
4. 汇总 ``/positions/history``（或 ``/positions-history``）返回的历史仓位已实现 PnL，
   并列出“已 claim 且价格归零”的市场以便复核。

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
    params = {"user": user, "limit": limit}
    endpoints = [
        f"{DATA_API_HOST}/positions/history",
        f"{DATA_API_HOST}/positions-history",
    ]
    last_error: Optional[Exception] = None
    for url in endpoints:
        try:
            return _paginate(url, params, max_pages)
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


def _summarize_history(entries: Iterable[Dict[str, Any]]) -> HistorySummary:
    realized = 0.0
    pct_values: List[float] = []
    zero_markets: List[Dict[str, Any]] = []
    count = 0
    for entry in entries:
        count += 1
        realized += _safe_float(entry.get("cashPnl") or entry.get("realizedPnl"))
        percent = (
            entry.get("percentPnl")
            or entry.get("percent_pnl")
            or entry.get("percentReturn")
        )
        pct_val = _safe_float(percent, default=0.0)
        has_pct = percent not in (None, "", [], {})
        if has_pct:
            pct_values.append(pct_val)

        cur_price = _safe_float(
            entry.get("curPrice")
            or entry.get("markPrice")
            or entry.get("avgPrice")
            or entry.get("finalPrice"),
            default=0.0,
        )
        status_text = str(entry.get("status") or entry.get("state") or "").lower()
        claimed_flag = bool(
            entry.get("claimed")
            or entry.get("isClaimed")
            or entry.get("redeemed")
            or entry.get("claimTx")
            or status_text in {"claimed", "redeemed", "closed"}
        )
        size_now = _safe_float(entry.get("size") or entry.get("quantity"))
        if (cur_price <= 0.0001 and claimed_flag) or (claimed_flag and size_now <= 0):
            zero_markets.append(
                {
                    "title": entry.get("title") or entry.get("market") or entry.get("slug"),
                    "outcome": entry.get("outcome"),
                    "cashPnl": _safe_float(entry.get("cashPnl") or entry.get("realizedPnl")),
                    "percentPnl": pct_val if has_pct else None,
                    "claimTx": entry.get("claimTx"),
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
    )


def _fmt_money(value: float) -> str:
    return _vp_fmt_money(value)


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
    parser.add_argument("--history-limit", type=int, default=500, help="历史仓位每页条数")
    parser.add_argument("--history-pages", type=int, default=5, help="历史仓位最大翻页次数")
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

    try:
        trades = _fetch_trades(
            user,
            limit=max(1, args.trades_limit),
            max_pages=max(1, args.trades_pages),
        )
    except Exception as exc:
        print(f"[ERR] 获取历史成交失败：{exc}", file=sys.stderr)
        return 4

    trade_summary = _summarize_trades(trades)

    try:
        history_entries = _fetch_positions_history(
            user,
            limit=max(1, args.history_limit),
            max_pages=max(1, args.history_pages),
        )
    except Exception as exc:
        print(f"[WARN] 获取历史仓位失败：{exc}")
        history_entries = []

    history_summary = _summarize_history(history_entries)

    if args.json:
        output = {
            "wallet": user,
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
    _print_zero_markets(history_summary.zero_markets)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
