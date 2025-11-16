
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Polymarket 历史成交与仓位盈亏统计脚本（EOA 优先版）。

功能概览：
1. 自动识别当前钱包地址（沿用 ``view_positions_EOA`` 的逻辑）。
2. 拉取 Data-API ``/positions`` 查看当前持仓。
3. 统计 Data-API ``/trades`` 返回的历史成交（买入数量 / 金额 / 均价等）。
4. 使用文档支持的 ``/activity`` 与 ``/closed-positions`` 端点提取历史
   仓位结算 / 赔付 / 领取信息，并列出“已 claim 且价格归零”的市场以便
   复核，避免访问未公开的 API。

使用示例：
    python3 history_positions_summary_EOA.py
    python3 history_positions_summary_EOA.py --json  # 以 JSON 输出到标准输出
    python3 history_positions_summary_EOA.py --json positions.json  # 直接保存到文件
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple, Set

import requests

from key_utils import make_key


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
MARKET_LOOKUP_HOSTS = [host for host in (GAMMA_API_HOST, GAMMA_ALT_HOST) if host]
DISABLE_MARKET_CACHE = bool(os.environ.get("DISABLE_MARKET_CACHE"))
DEBUG_LOG = False

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


def _optional_float(value: Any) -> Optional[float]:
    """在可选字段上使用的浮点解析，不会把缺失值当成 0。"""

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


def _coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                return []
        return [text]
    return []


def _extract_clob_ids(raw: Dict[str, Any]) -> List[str]:
    candidates = (
        raw.get("clobTokenIds"),
        raw.get("clob_token_ids"),
        raw.get("clobTokens"),
        raw.get("tokenIds"),
    )
    for cand in candidates:
        arr = [str(x).strip() for x in _coerce_list(cand) if str(x).strip()]
        if arr:
            return arr
    return []


def _extract_outcome_names(raw: Dict[str, Any]) -> List[str]:
    candidates = (
        raw.get("outcomes"),
        raw.get("outcomeNames"),
        raw.get("outcome_names"),
        raw.get("outcomeLabels"),
        raw.get("outcome_labels"),
    )
    for cand in candidates:
        arr = [str(x).strip() for x in _coerce_list(cand) if str(x).strip()]
        if arr:
            return arr
    return []


_MARKET_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


def _debug_print(message: str) -> None:
    if DEBUG_LOG:
        print(f"[DEBUG] {message}")


def _fetch_market_by_token(token_id: str) -> Optional[Dict[str, Any]]:
    token_id = str(token_id or "").strip()
    if not token_id:
        return None
    if not DISABLE_MARKET_CACHE:
        cached = _MARKET_CACHE.get(token_id)
        if cached is not None:
            _debug_print(f"market cache hit for token_id={token_id}")
            return cached
    else:
        _debug_print(f"market cache bypassed for token_id={token_id}")
    for host in MARKET_LOOKUP_HOSTS:
        if not host:
            continue
        try:
            resp = requests.get(
                f"{host}/markets",
                params={"clob_token_ids": token_id},
                timeout=20,
            )
            resp.raise_for_status()
            items, _ = _extract_items(resp.json())
        except Exception:
            continue
        if not items:
            continue
        market = items[0]
        if not DISABLE_MARKET_CACHE:
            _MARKET_CACHE[token_id] = market
        _debug_print(
            f"market fetched for token_id={token_id} from host={host} | title={market.get('title') or market.get('question') or market.get('name')}"
        )
        return market
    if not DISABLE_MARKET_CACHE:
        _MARKET_CACHE[token_id] = None
    return None


def _lookup_markets_for_assets(assets: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    seen: Set[str] = set()
    for asset in assets:
        token_id = str(asset or "").strip()
        if not token_id or token_id in seen:
            continue
        seen.add(token_id)
        market = _fetch_market_by_token(token_id)
        if market:
            out[token_id] = market
    return out


def _normalize_outcome_name(text: Any) -> str:
    if text is None:
        return ""
    normalized = str(text).strip().lower()
    if normalized in {"yes", "y", "1", "true"}:
        return "yes"
    if normalized in {"no", "n", "0", "false"}:
        return "no"
    return normalized


def _resolve_token_meta(asset: str, market: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    meta = {
        "token_index": None,
        "token_label": "",
        "token_side": "",
        "market_win_outcome": "",
        "market_slug": "",
        "market_title": "",
        "market_resolved": False,
    }
    if not market:
        return meta

    meta["market_slug"] = (
        market.get("slug")
        or market.get("marketSlug")
        or market.get("conditionSlug")
        or ""
    )
    meta["market_title"] = (
        market.get("question")
        or market.get("title")
        or market.get("name")
        or meta["market_title"]
    )
    meta["market_win_outcome"] = (
        market.get("winningOutcome")
        or market.get("resolvedOutcome")
        or market.get("resolveOutcome")
        or market.get("result")
        or ""
    )

    status_text = str(market.get("status") or market.get("state") or "").lower()
    resolved_ts = _normalize_timestamp(
        _first_present(
            market,
            (
                "resolvedTime",
                "resolveTime",
                "resolutionTime",
                "resolvedAt",
                "closedTime",
                "closedAt",
                "endTime",
                "endDate",
                "expiry",
                "expiration",
            ),
        )
    )
    closed_flag = bool(market.get("closed") or market.get("isClosed"))
    uma_status = str(market.get("umaResolutionStatus") or "").lower()
    meta["market_resolved"] = bool(
        meta["market_win_outcome"]
        or closed_flag
        or (resolved_ts is not None)
        or "resolve" in uma_status
        or "settle" in uma_status
        or "resolved" in status_text
        or "closed" in status_text
    )

    clob_ids = _extract_clob_ids(market)
    outcome_names = _extract_outcome_names(market)
    asset_str = str(asset or "").strip()
    if asset_str and clob_ids and asset_str in clob_ids:
        idx = clob_ids.index(asset_str)
        meta["token_index"] = idx
        if idx < len(outcome_names):
            meta["token_label"] = outcome_names[idx]
        elif idx == 0:
            meta["token_label"] = "Yes"
        elif idx == 1:
            meta["token_label"] = "No"
    tokens_obj = market.get("tokens") or market.get("tokenInfo") or market.get("token_info")
    token_entries: List[Dict[str, Any]] = []
    if isinstance(tokens_obj, list):
        token_entries = [t for t in tokens_obj if isinstance(t, dict)]
    elif isinstance(tokens_obj, dict):
        token_entries = [t for t in tokens_obj.values() if isinstance(t, dict)]
    for entry in token_entries:
        tid = entry.get("tokenId") or entry.get("token_id") or entry.get("id")
        tid = str(tid or "").strip()
        if tid and tid == asset_str:
            meta["token_label"] = (
                entry.get("outcome")
                or entry.get("outcomeName")
                or entry.get("name")
                or entry.get("label")
                or meta["token_label"]
            )
            meta["token_side"] = entry.get("type") or entry.get("side") or entry.get("position") or ""
            break

    # 在缺乏显式赢家字段时，用 Gamma 文档明确提供的 outcomes + outcomePrices 推断赢家。
    if not meta["market_win_outcome"]:
        price_candidates = market.get("outcomePrices") or market.get("outcome_prices")
        price_list: List[Optional[float]] = []
        for item in _coerce_list(price_candidates):
            price_list.append(_optional_float(item))

        # 仅在市场已关闭或标记为已结算时尝试推断，避免进行中的市场被误判。
        resolved_like = meta["market_resolved"]

        if resolved_like and outcome_names and price_list and len(outcome_names) == len(price_list):
            winners_idx = [i for i, price in enumerate(price_list) if isinstance(price, float) and price >= 0.99]
            if not winners_idx:
                # 若没有明显的 ~1.0 价格，则选取价格最高的 outcome 作为候选。
                max_price = max([p for p in price_list if isinstance(p, float)], default=None)
                if isinstance(max_price, float) and max_price >= 0.5:
                    winners_idx = [i for i, price in enumerate(price_list) if price == max_price]

            if winners_idx:
                winners = [outcome_names[i] for i in winners_idx]
                if meta["token_index"] in winners_idx and meta["token_label"]:
                    meta["market_win_outcome"] = meta["token_label"]
                else:
                    meta["market_win_outcome"] = winners[0]
    return meta


def _market_resolution_timestamp(market: Optional[Dict[str, Any]]) -> Optional[float]:
    if not isinstance(market, dict):
        return None
    return _normalize_timestamp(
        _first_present(
            market,
            (
                "resolvedTime",
                "resolveTime",
                "resolutionTime",
                "resolvedAt",
                "closedTime",
                "closedAt",
                "endTime",
                "endDate",
                "expiry",
                "expiration",
            ),
        )
    )

def _fetch_trades(user: str, limit: int, max_pages: int) -> List[Dict[str, Any]]:
    """拉取用户的历史成交数据，分页获取"""
    # Now using the correct DATA_API_HOST for /trades endpoint
    trades_endpoint = f"{DATA_API_HOST}/trades"
    params = {"user": user, "limit": limit}
    return _paginate(trades_endpoint, params, max_pages)


def _fetch_activity(user: str, limit: int, max_pages: int) -> List[Dict[str, Any]]:
    endpoint = f"{DATA_API_HOST}/activity"
    params = {"user": user, "limit": limit}
    entries = _paginate(endpoint, params, max_pages)
    for entry in entries:
        entry.setdefault("_source", endpoint)
    return entries


def _fetch_closed_positions(user: str, limit: int, max_pages: int) -> List[Dict[str, Any]]:
    """从官方文档的 /closed-positions 接口获取历史仓位。"""

    endpoint = f"{DATA_API_HOST}/closed-positions"
    params = {"user": user}
    entries = _paginate_offset(endpoint, params, limit, max_pages)
    for entry in entries:
        entry.setdefault("_source", endpoint)
    return entries


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


def _log_history_stats(tag: str, entries: List[Dict[str, Any]]) -> None:
    if not DEBUG_LOG:
        return

    ts_values: List[float] = []
    claim_like = 0
    resolved_like = 0
    for entry in entries:
        ts = _entry_timestamp(entry)
        if ts is not None:
            ts_values.append(ts)
        if entry.get("claimAmount") or entry.get("claimed") or entry.get("wasClaimed"):
            claim_like += 1
        status_text = str(entry.get("status") or entry.get("type") or entry.get("action") or "").lower()
        if status_text in ("claim", "claimed", "resolve", "resolved", "settled", "closed"):
            resolved_like += 1

    latest_ts = max(ts_values) if ts_values else None
    earliest_ts = min(ts_values) if ts_values else None
    latest_text = _fmt_timestamp_local(latest_ts) if latest_ts is not None else "-"
    earliest_text = _fmt_timestamp_local(earliest_ts) if earliest_ts is not None else "-"
    _debug_print(
        f"{tag}: total={len(entries)} | with_ts={len(ts_values)} | earliest={earliest_text} | latest={latest_text} | claim_like={claim_like} | resolved_like={resolved_like}"
    )


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


def _summarize_trade_cashflow(trades: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """按照 token 统计买卖成交的数量与现金流。"""

    summary: Dict[str, Dict[str, float]] = {}
    for trade in trades:
        asset = _extract_asset_id(trade)
        if not asset:
            continue
        side = (trade.get("side") or "").upper()
        size = _safe_float(trade.get("size"))
        price = _safe_float(trade.get("price"))
        bucket = summary.setdefault(
            asset,
            {
                "buy_size_total": 0.0,
                "buy_cost_total": 0.0,
                "sell_size_total": 0.0,
                "sell_proceeds_total": 0.0,
            },
        )
        if side == "BUY":
            bucket["buy_size_total"] += size
            bucket["buy_cost_total"] += size * price
        elif side == "SELL":
            bucket["sell_size_total"] += size
            bucket["sell_proceeds_total"] += size * price
    return summary


def _summarize_activity(entries: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    resolved: Dict[str, Dict[str, Any]] = {}
    claim_keys = (
        "claimAmount",
        "amountClaimed",
        "claimedAmount",
        "payout",
        "totalPayout",
        "payoutAmount",
    )
    settle_keys = ("settlementPrice", "settledPrice", "resolvePrice")

    for entry in entries:
        asset = _extract_asset_id(entry)
        if not asset:
            continue
        ts = _entry_timestamp(entry)
        claim_value = _first_present(entry, claim_keys)
        settlement_value = _first_present(entry, settle_keys)

        claim_amount = _optional_float(claim_value)
        settlement_price = _optional_float(settlement_value)
        status_text = (entry.get("status") or entry.get("type") or entry.get("action") or "").lower()
        source_text = str(entry.get("_source") or entry.get("source") or "")
        is_closed_positions = "closed-positions" in source_text

        claimed_flag = bool(
            entry.get("claimed")
            or entry.get("isClaimed")
            or entry.get("wasClaimed")
            or status_text in ("claim", "claimed", "redeem", "redeemed")
            or (is_closed_positions and (claim_amount is not None and claim_amount >= 0))
        )
        resolved_flag = bool(
            entry.get("resolved")
            or entry.get("isResolved")
            or entry.get("status") == "resolved"
            or status_text in ("resolve", "resolved", "settled", "closed")
            or is_closed_positions
        )

        # 对于纯交易记录（无 resolved/claim 信号且无结算金额），跳过，避免“TRADE”之类的状态遮挡结算数据。
        if not (
            resolved_flag
            or claimed_flag
            or claim_amount is not None
            or settlement_price is not None
        ):
            continue

        priority = 0
        if claimed_flag:
            priority = 4
        elif is_closed_positions and (claim_amount is not None or settlement_price is not None):
            priority = 3
        elif claim_amount is not None and claim_amount > 0:
            priority = 3
        elif resolved_flag or settlement_price is not None:
            priority = 2
        else:
            priority = 1

        # 注意：/activity 等接口中的 ``outcome`` 字段通常表示“用户买入的方向”，
        # 而非市场最终结果。如果把它当成 resolved outcome，会把亏损仓位误判为
        # 中奖（例如买了 "No"，activity 里 outcome=No，但市场实际结算为 Yes）。
        # 因此只信任显式的 resolved / winning 相关字段。
        info = {
            "asset": asset,
            "status": entry.get("status") or entry.get("type") or entry.get("action") or "",
            # 仅保留显式的 resolved / winning 字段，不再从 activity outcome 猜测。
            "resolved_outcome": entry.get("winningOutcome")
            or entry.get("resolvedOutcome")
            or entry.get("resolveOutcome"),
            "cash_pnl": _extract_cash_pnl(entry),
            "claim_amount": claim_amount,
            "settlement_price": settlement_price,
            "timestamp": ts,
            "was_claimed": claimed_flag,
            "priority": priority,
            "source": source_text,
            "raw": entry,
        }
        info["is_resolved"] = bool(
            resolved_flag
            or claimed_flag
            or settlement_price is not None
            or claim_amount is not None
        )
        prev = resolved.get(asset)
        prev_ts = prev.get("timestamp") if prev else None
        prev_priority = prev.get("priority") if prev else None
        should_replace = False
        if prev is None:
            should_replace = True
        else:
            if prev_priority is None or priority > prev_priority:
                should_replace = True
            elif priority == prev_priority:
                if ts is not None and (prev_ts is None or ts >= prev_ts):
                    should_replace = True
        if should_replace:
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
    markets: Dict[str, Dict[str, Any]],
    trade_cashflow: Dict[str, Dict[str, float]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for asset, bucket in positions.items():
        realized_entry = realized.get(asset)
        market_meta = markets.get(asset)
        token_meta = _resolve_token_meta(asset, market_meta)
        resolved_ts = None
        if realized_entry and realized_entry.get("is_resolved") and realized_entry.get(
            "timestamp"
        ) is not None:
            resolved_ts = realized_entry.get("timestamp")
        elif token_meta.get("market_resolved"):
            resolved_ts = _market_resolution_timestamp(market_meta)
        resolved_outcome = token_meta.get("market_win_outcome") or None
        resolution_status = None
        if realized_entry and realized_entry.get("is_resolved"):
            resolution_status = realized_entry.get("status")
        if not resolution_status:
            resolution_status = "已结算" if token_meta.get("market_resolved") else None
        outcome_label = bucket.outcome or token_meta.get("token_label") or ""
        key = make_key(bucket.condition_id, token_meta.get("token_index"), outcome_label)
        trade_stats = trade_cashflow.get(asset, {})
        rows.append(
            {
                "key": key,
                "token_id": asset,
                "asset": asset,
                "title": bucket.title or token_meta.get("market_title") or "",
                "outcome": outcome_label,
                "side": token_meta.get("token_side") or None,
                "marketSlug": bucket.market_slug or token_meta.get("market_slug") or "",
                "conditionId": bucket.condition_id,
                "icon": bucket.icon,
                "totalSize": bucket.total_size,
                "avgEntryPrice": bucket.avg_price,
                "totalCost": bucket.total_cost,
                "buy_size_total": trade_stats.get("buy_size_total", 0.0),
                "buy_cost_total": trade_stats.get("buy_cost_total", 0.0),
                "sell_size_total": trade_stats.get("sell_size_total", 0.0),
                "sell_proceeds_total": trade_stats.get("sell_proceeds_total", 0.0),
                "firstBuyTime": bucket.first_ts,
                "lastBuyTime": bucket.last_ts,
                "resolutionTime": resolved_ts,
                "resolutionStatus": resolution_status,
                "resolvedOutcome": resolved_outcome,
                "resolvedOutcomeSource": "market" if resolved_outcome else None,
                "realizedPnl": realized_entry.get("cash_pnl") if realized_entry else None,
                "claimAmount": realized_entry.get("claim_amount") if realized_entry else None,
                "settlementPrice": realized_entry.get("settlement_price") if realized_entry else None,
                "isResolved": bool(
                    (realized_entry.get("is_resolved") if realized_entry else None)
                    or resolved_outcome
                    or token_meta.get("market_resolved")
                ),
                "wasClaimed": realized_entry.get("was_claimed") if realized_entry else False,
                "tokenOutcomeLabel": token_meta.get("token_label") or bucket.outcome or "",
                "tokenOutcomeIndex": token_meta.get("token_index"),
                "tokenOutcomeSide": token_meta.get("token_side") or "",
            }
        )
    for row in rows:
        settlement_price = row.get("settlementPrice")
        total_size = row.get("totalSize") or 0.0
        total_cost = row.get("totalCost") or 0.0
        payout = None
        derived_pnl = None
        outcome_match = None
        if isinstance(settlement_price, (int, float)):
            payout = total_size * float(settlement_price)
            derived_pnl = payout - total_cost
        else:
            token_outcome = row.get("tokenOutcomeLabel")
            resolved_outcome = row.get("resolvedOutcome")
            norm_token = _normalize_outcome_name(token_outcome)
            norm_resolved = _normalize_outcome_name(resolved_outcome)
            if norm_token and norm_resolved:
                outcome_match = norm_token == norm_resolved
                payout = total_size if outcome_match else 0.0
                derived_pnl = payout - total_cost
        row["derivedPayout"] = payout
        row["derivedPnl"] = derived_pnl
        row["derivedOutcomeMatch"] = outcome_match
        realized_pnl = row.get("realizedPnl")
        has_reliable_derivation = isinstance(derived_pnl, (int, float)) and (
            isinstance(payout, (int, float)) or outcome_match is not None
        )
        if has_reliable_derivation:
            if not isinstance(realized_pnl, (int, float)):
                row["realizedPnl"] = derived_pnl
            else:
                if abs(realized_pnl - derived_pnl) > 1e-6:
                    row["realizedPnl"] = derived_pnl
    rows.sort(key=lambda r: (r.get("lastBuyTime") or 0), reverse=True)
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Polymarket 历史仓位统计工具")
    parser.add_argument(
        "--json",
        nargs="?",
        const="-",
        dest="json_out",
        help="以 JSON 输出结果；可选路径，默认输出到标准输出",
    )
    parser.add_argument("--debug", action="store_true", help="输出调试日志，包含接口回包统计")
    parser.add_argument(
        "--no-market-cache",
        action="store_true",
        help="跳过市场元数据缓存，强制每次请求最新市场信息",
    )
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

    global DEBUG_LOG, DISABLE_MARKET_CACHE
    DEBUG_LOG = bool(args.debug)
    if args.no_market_cache:
        DISABLE_MARKET_CACHE = True
        _MARKET_CACHE.clear()
        _debug_print("market cache disabled by CLI flag")

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
    _debug_print(f"/trades 返回 {len(trades)} 条记录（含 BUY/SELL）")

    history_entries: List[Dict[str, Any]] = []
    try:
        history_entries.extend(
            _fetch_activity(
                user,
                limit=max(1, args.history_limit),
                max_pages=max(1, args.history_pages),
            )
        )
    except Exception as exc:
        print(f"[WARN] 获取历史仓位 /activity 失败：{exc}")

    _log_history_stats("/activity", history_entries)

    try:
        history_entries.extend(
            _fetch_closed_positions(
                user,
                limit=max(1, args.history_limit),
                max_pages=max(1, args.history_pages),
            )
        )
    except Exception as exc:
        print(f"[WARN] 获取 /closed-positions 失败：{exc}")

    _log_history_stats("/closed-positions", history_entries)

    filtered_trades = _filter_entries_since(trades, since_ts)
    filtered_history = _filter_entries_since(history_entries, since_ts)

    _debug_print(
        f"过滤时间 {since_date_text} 后：trades={len(filtered_trades)} | history={len(filtered_history)}"
    )

    buy_positions = _summarize_buy_trades(filtered_trades)
    trade_cashflow = _summarize_trade_cashflow(filtered_trades)
    realized_map = _summarize_activity(filtered_history)
    market_meta = _lookup_markets_for_assets(buy_positions.keys())
    rows = _compose_position_rows(buy_positions, realized_map, market_meta, trade_cashflow)

    if args.json_out is not None:
        output = {
            "wallet": user,
            "since_date_utc8": since_date_text,
            "positions": rows,
            "trades": filtered_trades,
            "trade_cashflow": trade_cashflow,
        }
        serialized = json.dumps(output, ensure_ascii=False, indent=2)
        json_path = args.json_out
        if not json_path or json_path == "-":
            print(serialized)
        else:
            if not os.path.isabs(json_path):
                script_dir = os.path.abspath(os.path.dirname(__file__))
                json_path = os.path.join(script_dir, json_path)
            with open(json_path, "w", encoding="utf-8") as f:
                f.write(serialized)
            print(f"[OK] JSON 已保存：{json_path}")
        return 0

    if not rows:
        print("[INFO] 在指定区间内没有买入记录。")
        return 0

    print("\n[HISTORY] 历史买入持仓（含结算盈亏）：")
    total_entries = len(rows)
    success_count = 0
    failure_count = 0
    total_invest = 0.0
    total_profit = 0.0
    unresolved_but_marked = []

    for idx, row in enumerate(rows, 1):
        total_size = row.get("totalSize") or 0.0
        avg_price = row.get("avgEntryPrice") or 0.0
        total_cost = row.get("totalCost") or 0.0
        total_invest += total_cost
        realized_pnl = row.get("realizedPnl")
        resolution_status = row.get("resolutionStatus")
        if not resolution_status:
            resolution_status = "已结算" if row.get("isResolved") else "未结算"
        resolved_outcome = row.get("resolvedOutcome") or "-"
        first_ts = _fmt_timestamp_local(row.get("firstBuyTime"))
        last_ts = _fmt_timestamp_local(row.get("lastBuyTime"))
        resolution_time = _fmt_timestamp_local(row.get("resolutionTime"))
        token_label = row.get("tokenOutcomeLabel") or "-"
        token_index = row.get("tokenOutcomeIndex")
        token_side = row.get("tokenOutcomeSide") or ""
        option_desc = token_label
        if token_index is not None:
            option_desc = f"{option_desc} (idx={token_index})"
        if token_side:
            option_desc = f"{option_desc} [{token_side}]"
        print(
            f"{idx:>2}. {row.get('title') or '-'} | {row.get('outcome') or '-'} | token_id={row.get('asset')}"
        )
        print(
            "    "
            f"买入方向=BUY | 买入总量={total_size:.4f} | 均价={_vp_fmt_money(avg_price)} | 总成本≈{_vp_fmt_money(total_cost)}"
        )
        print(f"    买入选项={option_desc}")
        print(f"    买入时间区间：{first_ts} -> {last_ts}")
        print(
            "    "
            f"结算状态={resolution_status} | 结算结果={resolved_outcome} | 结算时间={resolution_time}"
        )
        derived_payout = row.get("derivedPayout")
        derived_pnl = row.get("derivedPnl")
        outcome_match = row.get("derivedOutcomeMatch")
        if isinstance(realized_pnl, (int, float)):
            total_profit += realized_pnl
        if isinstance(derived_pnl, (int, float)) and isinstance(derived_payout, (int, float)):
            payout_text = _vp_fmt_money(derived_payout)
            derived_text = _vp_fmt_money(derived_pnl)
            match_text = "命中" if outcome_match else "失利"
            if outcome_match is True:
                success_count += 1
            elif outcome_match is False:
                failure_count += 1
            print(
                "    "
                f"推导结算：{match_text} | 理论赔付≈{payout_text} | 推导盈亏≈{derived_text}"
            )
        else:
            print("    推导结算：-")

        if row.get("isResolved") and not row.get("resolvedOutcome"):
            unresolved_but_marked.append(
                {
                    "title": row.get("title") or row.get("marketSlug") or row.get("asset"),
                    "asset": row.get("asset"),
                    "reason": "市场元数据表明已结算，但未能确定赢家（缺少 winningOutcome/outcomePrices）",
                }
            )

        print()

    print("\n[SUMMARY] 统计概览：")
    roi = (total_profit / total_invest * 100) if total_invest > 0 else 0.0
    print(
        f"总条目={total_entries} | 命中={success_count} | 失利={failure_count}"
    )
    print(
        "总投入≈{} | 总收益≈{} | 总收益率≈{:.2f}%".format(
            _vp_fmt_money(total_invest), _vp_fmt_money(total_profit), roi
        )
    )

    if unresolved_but_marked:
        print("\n[WARN] 以下市场被标记为已结算，但未能从 Gamma 数据推断赢家：")
        for item in unresolved_but_marked:
            print(
                f" - {item.get('title') or '-'} (token_id={item.get('asset')}) | {item.get('reason')}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
