#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Volatility_fliter_EOA.py  ·  REST-only 极简版
- 仅用 REST /books 批量获取买一/卖一（bestBid/bestAsk），完全移除 WS 逻辑
- 保留：时间切片（突破500）、早筛后回补、流式逐个输出/详细块、诊断样本等
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except Exception as e:
    print("[FATAL] 缺少 requests，请先 pip install requests", file=sys.stderr)
    raise

# -------------------------------
# （可选）REST 客户端，仅用于打印 API key 前缀（非必需）
# -------------------------------

def _import_rest_client():
    try:
        from Volatility_arbitrage_main_rest_EOA import get_client as _get_client
        return _get_client
    except Exception as e:
        print(f"[WARN] 无法加载 REST 客户端：{e}", file=sys.stderr)
        def _noop():
            return None
        return _noop

get_rest_client = _import_rest_client()

# -------------------------------
# 小工具
# -------------------------------

def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _parse_dt(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None

def _coerce_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, str):
            x2 = x.replace(',', '').strip()
            if x2 == '':
                return None
            return float(x2)
    except Exception:
        return None
    return None

def _coerce_bool(x: Any) -> Optional[bool]:
    if x is None:
        return None
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        s = x.strip().lower()
        if s in ('true', 'yes', 'y', '1'):
            return True
        if s in ('false', 'no', 'n', '0'):
            return False
    if isinstance(x, (int, float)):
        return bool(x)
    return None

def _fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "-"
    try:
        return f"{x:,.2f}"
    except Exception:
        return str(x)

def _hours_until(t: Optional[dt.datetime]) -> Optional[float]:
    if not t:
        return None
    delta = (t - _now_utc()).total_seconds() / 3600.0
    return round(delta, 1)

def _infer_binary_from_raw(raw: Dict[str, Any]) -> bool:
    if not isinstance(raw, dict):
        return False
    op = raw.get("outcomePrices")
    if isinstance(op, list) and len(op) == 2:
        return True
    for k in ("outcomes", "contracts"):
        v = raw.get(k)
        if isinstance(v, list) and len(v) == 2:
            return True
    for k in ("binary", "isBinary"):
        bv = raw.get(k)
        if isinstance(bv, bool) and bv:
            return True
        if isinstance(bv, str) and bv.lower() in ("true","yes","y","1"):
            return True
    return False

# -------------------------------
# 数据结构
# -------------------------------

@dataclass
class OutcomeSnapshot:
    name: str
    token_id: Optional[str] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None

@dataclass
class MarketSnapshot:
    slug: str
    title: str
    raw: Dict[str, Any] = field(default_factory=dict)
    yes: OutcomeSnapshot = field(default_factory=lambda: OutcomeSnapshot(name='YES'))
    no: OutcomeSnapshot = field(default_factory=lambda: OutcomeSnapshot(name='NO'))
    liquidity: Optional[float] = None
    volume24h: Optional[float] = None
    totalVolume: Optional[float] = None
    tags: List[str] = field(default_factory=list)
    active: Optional[bool] = None
    closed: Optional[bool] = None
    resolved: Optional[bool] = None
    acceptingOrders: Optional[bool] = None
    end_time: Optional[dt.datetime] = None

# -------------------------------
# Gamma 抓取（时间切片 · 突破500）
# -------------------------------

_GAMMA_HOST = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")

def _gamma_fetch(params: Dict[str, str]) -> List[Dict[str, Any]]:
    url = f"{_GAMMA_HOST}/markets"
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []
    except Exception:
        return []

def fetch_markets_windowed(end_min: dt.datetime, end_max: dt.datetime, window_days: int = 14) -> List[Dict[str, Any]]:
    all_mkts: List[Dict[str, Any]] = []
    seen: set = set()
    cur = end_min
    one_sec = dt.timedelta(seconds=1)

    while cur <= end_max:
        sub_end = min(cur + dt.timedelta(days=window_days), end_max)
        params = {
            "limit": "500",
            "order": "endDate",
            "ascending": "true",
            "active": "true",
            "closed": "false",
            "end_date_min": cur.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": sub_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        chunk = _gamma_fetch(params)

        for m in chunk:
            mid = m.get("id") or m.get("slug")
            if mid and mid not in seen:
                seen.add(mid)
                all_mkts.append(m)

        if len(chunk) >= 500 and window_days > 1:
            half = max(1, window_days // 2)
            sub = fetch_markets_windowed(cur, sub_end, window_days=half)
            for m in sub:
                mid = m.get("id") or m.get("slug")
                if mid and mid not in seen:
                    seen.add(mid)
                    all_mkts.append(m)

        cur = sub_end + one_sec

    return all_mkts

# -------------------------------
# 解析 + 旧格式检测
# -------------------------------

def _is_arch_legacy_nonclob(raw: Dict[str, Any], legacy_end_days: int) -> bool:
    title = (raw.get("question") or raw.get("title") or "").strip()
    slug  = (raw.get("slug") or "").strip()
    end   = _parse_dt(raw.get("endDate") or raw.get("end_time") or raw.get("endTime"))
    clob_ids = raw.get("clobTokenIds") or raw.get("clob_token_ids") or raw.get("clobTokens")

    if title.upper().startswith("ARCH") or slug.lower().startswith("arch"):
        return True
    if not clob_ids:
        return True
    if end is not None and legacy_end_days and legacy_end_days > 0:
        try:
            hours = _hours_until(end)
            if hours is not None and hours < -24.0 * float(legacy_end_days):
                return True
        except Exception:
            pass
    return False

def _parse_market(raw: Dict[str, Any]) -> MarketSnapshot:
    title = raw.get("question") or raw.get("title") or ""
    slug  = raw.get("slug") or ""
    ms = MarketSnapshot(slug=slug, title=title, raw=raw)

    ms.active = _coerce_bool(raw.get("active"))
    ms.closed = _coerce_bool(raw.get("closed"))
    ms.resolved = _coerce_bool(raw.get("resolved"))
    ms.acceptingOrders = _coerce_bool(raw.get("acceptingOrders"))
    ms.end_time = _parse_dt(raw.get("endDate") or raw.get("end_time") or raw.get("endTime"))

    ms.liquidity = _coerce_float(raw.get("liquidity") or raw.get("liquidity_num") or raw.get("liquidityNum") or raw.get("liquidityUsd") or raw.get("totalLiquidity"))
    ms.volume24h = _coerce_float(raw.get("volume24h") or raw.get("volume24Hr") or raw.get("volume24Hour") or raw.get("volume_24h") or raw.get("lastDayVolume"))
    ms.totalVolume = _coerce_float(raw.get("totalVolume") or raw.get("volume") or raw.get("volume_num") or raw.get("volumeNum"))

    tags = raw.get("tags") or raw.get("tagNames") or raw.get("categories") or []
    if isinstance(tags, list):
        ms.tags = [str(t) for t in tags]
    elif isinstance(tags, str):
        ms.tags = [tags]

    clob_ids = raw.get("clobTokenIds") or raw.get("clob_token_ids") or raw.get("clobTokens")
    if isinstance(clob_ids, str):
        try:
            import json as _json
            clob_ids = _json.loads(clob_ids)
        except Exception:
            clob_ids = None
    if isinstance(clob_ids, list) and len(clob_ids) >= 2:
        try:
            ms.yes.token_id = str(clob_ids[0])
            ms.no.token_id  = str(clob_ids[1])
        except Exception:
            pass

    return ms

# -------------------------------
# 早筛（不拉价格，先确定是否需要回补）
# -------------------------------

def _is_binary(ms: MarketSnapshot) -> bool:
    return bool(ms.yes.token_id and ms.no.token_id)

def _early_filter_reason(ms: MarketSnapshot, min_end_hours: float, legacy_end_days: int) -> Tuple[bool, str]:
    if _is_arch_legacy_nonclob(ms.raw, legacy_end_days):
        if not _is_binary(ms) and _infer_binary_from_raw(ms.raw):
            return False, "二元（旧格式；缺 clobTokenIds）"
        return False, "归档/旧格式（非 CLOB）"
    if not _is_binary(ms):
        if _infer_binary_from_raw(ms.raw):
            return False, "二元（旧格式；缺 clobTokenIds）"
        return False, "非二元市场"
    if min_end_hours is not None and min_end_hours > 0:
        h = _hours_until(ms.end_time)
        if h is None or h < min_end_hours:
            return False, f"剩余时间不足（{h}h）"
    return True, "候选（待回补报价）"

# -------------------------------
# REST /books 批量回补（直接取买一/卖一）
# -------------------------------

_POLY_HOST = os.environ.get("POLY_HOST", "https://clob.polymarket.com").rstrip("/")

def _rest_books_backfill(candidates: List[MarketSnapshot], batch_size: int = 200, timeout: float = 10.0) -> None:
    # 仅对仍缺买卖价的 token 做回补（任一侧有价即可跳过）
    missing: List[str] = []
    index: Dict[str, Tuple[MarketSnapshot, str]] = {}
    seen = set()

    for ms in candidates:
        for side, snap in (('YES', ms.yes), ('NO', ms.no)):
            tid = snap.token_id
            if not tid:
                continue
            if (snap.bid is None and snap.ask is None) and tid not in seen:
                seen.add(tid)
                missing.append(tid)
                index[tid] = (ms, side)

    if not missing:
        return

    url = f"{_POLY_HOST}/books"
    headers = {"Content-Type": "application/json"}

    def best_from_levels(levels: List[Dict[str, Any]], is_bid: bool) -> Optional[float]:
        if not isinstance(levels, list) or not levels:
            return None
        prices = []
        for lv in levels:
            p = _coerce_float((lv or {}).get("price"))
            if p is not None:
                prices.append(p)
        if not prices:
            return None
        return (max(prices) if is_bid else min(prices))

    for i in range(0, len(missing), batch_size):
        batch = missing[i:i+batch_size]
        body = [{"token_id": tid} for tid in batch]
        try:
            r = requests.post(url, json=body, headers=headers, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[WARN] REST /books 回补失败：{e}", file=sys.stderr)
            continue

        if not isinstance(data, list):
            continue
        for ob in data:
            try:
                tid = str(ob.get("asset_id") or ob.get("token_id") or "")
                if not tid or tid not in index:
                    continue
                ms, side = index[tid]
                bids = ob.get("bids") or []
                asks = ob.get("asks") or []
                bb = best_from_levels(bids, is_bid=True)
                aa = best_from_levels(asks, is_bid=False)
                if side == 'YES':
                    if ms.yes.bid is None and bb is not None: ms.yes.bid = bb
                    if ms.yes.ask is None and aa is not None: ms.yes.ask = aa
                else:
                    if ms.no.bid is None and bb is not None: ms.no.bid = bb
                    if ms.no.ask is None and aa is not None: ms.no.ask = aa
            except Exception:
                continue

# -------------------------------
# 最终筛选（在回补后判断报价）
# -------------------------------

def _final_pass_reason(ms: MarketSnapshot, require_quotes: bool) -> Tuple[bool, str]:
    if require_quotes:
        yes_ok = (ms.yes.bid is not None or ms.yes.ask is not None)
        no_ok  = (ms.no.bid is not None or ms.no.ask is not None)
        if not (yes_ok or no_ok):
            return False, "缺少买卖价（空簿/超时）"
    return True, "OK"

# -------------------------------
# 打印
# -------------------------------

def _print_snapshot(idx: int, total: int, ms: MarketSnapshot):
    print(f"[TRACE] [{idx}/{total}] 原始市场：slug={ms.slug} | 标题={ms.title}")
    st = " ".join([
        f"active={'是' if ms.active else '-'}",
        f"resolved={'是' if ms.resolved else '-'}",
        f"closed={'是' if ms.closed else '-'}",
        f"acceptingOrders={'是' if ms.acceptingOrders else '-'}",
    ])
    print(f"[TRACE]   状态：{st}")
    print(f"[TRACE]   金额：liquidity={_fmt_money(ms.liquidity)} volume24h={_fmt_money(ms.volume24h)} totalVolume={_fmt_money(ms.totalVolume)}")
    raw_end = ms.end_time.isoformat() if ms.end_time else "-"
    print(f"[TRACE]   时间：raw_end={raw_end}")
    print(f"[TRACE]   解析结果：")
    print(f"[TRACE]     {ms.slug} | {ms.title}")
    if (ms.yes.token_id is None or ms.no.token_id is None):
        print(f"[TRACE]       [HINT] 未能解析 clobTokenIds（疑似旧格式）。")
    def _fmt_side(s: OutcomeSnapshot) -> str:
        b = "-" if s.bid is None else f"{s.bid:.4f}"
        a = "-" if s.ask is None else f"{s.ask:.4f}"
        return f"{s.name}[{s.token_id}] bid={b} ask={a}"
    print(f"[TRACE]       {_fmt_side(ms.yes)}")
    print(f"[TRACE]       {_fmt_side(ms.no)}")
    h = _hours_until(ms.end_time)
    print(f"[TRACE]       liquidity={_fmt_money(ms.liquidity)}  volume24h={_fmt_money(ms.volume24h)}  ends_in={h}h  tags={','.join(ms.tags) if ms.tags else '-'}")

def _print_singleline(ms: MarketSnapshot, reason: str):
    yb = "-" if ms.yes.bid is None else f"{ms.yes.bid:.4f}"
    ya = "-" if ms.yes.ask is None else f"{ms.yes.ask:.4f}"
    nb = "-" if ms.no.bid is None else f"{ms.no.bid:.4f}"
    na = "-" if ms.no.ask is None else f"{ms.no.ask:.4f}"
    h  = _hours_until(ms.end_time)
    print(f"[RES] {ms.slug} | {ms.title} | YES {yb}/{ya} NO {nb}/{na} | ends_in={h}h | {reason}", flush=True)

# -------------------------------
# 主流程（含流式模式）
# -------------------------------

def main():
    ap = argparse.ArgumentParser(description="Polymarket 市场筛选（REST-only：/books 批量回补买一/卖一）")
    ap.add_argument("--books-batch-size", type=int, default=200, help="REST /books 批量回补的 token_id 数量上限（非流式模式）")
    ap.add_argument("--no-rest-backfill", action="store_true", help="关闭 REST 回补（诊断用，默认开启）")
    ap.add_argument("--skip-orderbook", action="store_true", help="跳过任何订单簿/价格回补（仅诊断）")
    ap.add_argument("--allow-illiquid", action="store_true", help="允许无报价市场通过（仅诊断）")

    ap.add_argument("--min-end-hours", type=float, default=24.0, help="仅抓取结束时间晚于该阈值（小时）的市场")
    ap.add_argument("--max-end-days", type=int, default=183, help="仅抓取结束时间在未来 N 天内的市场")
    ap.add_argument("--gamma-window-days", type=int, default=14, help="Gamma 时间切片的窗口大小（天），命中 500 会自动递归切分")

    ap.add_argument("--legacy-end-days", type=int, default=730, help="结束早于 N 天视为旧格式/归档（默认 730 天）")

    ap.add_argument("--diagnose", action="store_true", help="打印诊断信息（非流式模式下打印样本）")
    ap.add_argument("--diagnose-samples", type=int, default=30, help="诊断打印的样本数上限（非流式模式）")
    ap.add_argument("--only", type=str, default="", help="仅处理包含该子串的 slug/title（大小写不敏感）")

    # 流式输出选项
    ap.add_argument("--stream", action="store_true", help="启用流式逐个输出（按分片处理）")
    ap.add_argument("--stream-chunk-size", type=int, default=200, help="流式：每个分片的市场数量")
    ap.add_argument("--stream-books-batch-size", type=int, default=200, help="流式：每个分片内 REST /books 批量回补的 token_id 数量上限")
    ap.add_argument("--stream-verbose", action="store_true", help="流式：逐个输出详细块（默认仅单行）")
    args = ap.parse_args()

    # 仅用于展示 API key 前缀
    try:
        getc = get_rest_client
        rest_client = getc() if callable(getc) else None
        api_creds = getattr(rest_client, "api_creds", None)
        def g(x,k):
            if isinstance(x, dict): return x.get(k)
            return getattr(x,k,None)
        ak = g(api_creds, "api_key")
        if ak:
            print(f"[INFO] 已加载 EOA API credentials：{ak[:6]}***{ak[-4:]}")
    except Exception:
        pass

    # 仅抓未来盘：时间窗口 = [now + min_end_hours, now + max_end_days]
    now = _now_utc()
    end_min = now + dt.timedelta(hours=args.min_end_hours)
    end_max = now + dt.timedelta(days=args.max_end_days)

    mkts_raw = fetch_markets_windowed(end_min, end_max, window_days=args.gamma_window_days)
    print(f"[TRACE] 采用时间切片抓取完成：共获取 {len(mkts_raw)} 条（窗口={args.gamma_window_days} 天）")

    only_pat = args.only.lower().strip()

    # ---------- 流式模式 ----------
    if args.stream:
        total = len(mkts_raw)
        processed = 0
        chosen_cnt = 0
        for s in range(0, total, args.stream_chunk_size):
            chunk_raw = mkts_raw[s:s + args.stream_chunk_size]
            # 解析 + 早筛（即时输出被拒绝的理由）
            candidates: List[MarketSnapshot] = []
            for raw in chunk_raw:
                title = (raw.get("question") or raw.get("title") or "")
                slug  = (raw.get("slug") or "")
                if only_pat and (only_pat not in title.lower() and only_pat not in slug.lower()):
                    continue
                ms = _parse_market(raw)
                ok, reason = _early_filter_reason(ms, args.min_end_hours, args.legacy_end_days)
                if ok:
                    candidates.append(ms)
                else:
                    if args.stream_verbose:
                        _print_snapshot(processed+1, total, ms)
                        print(f"[TRACE]   -> 结果：{reason}。")
                        print(f"[TRACE]   --------------------------------------------------")
                    else:
                        _print_singleline(ms, reason)
                processed += 1

            # 分片内批量 REST 回补
            if not args.skip_orderbook and candidates and (not args.no_rest_backfill):
                _rest_books_backfill(candidates, batch_size=args.stream_books_batch_size)

            # 最终判定（即时输出）
            for ms in candidates:
                ok2, reason2 = _final_pass_reason(ms, require_quotes=(not args.allow_illiquid))
                if args.stream_verbose:
                    _print_snapshot(processed+1, total, ms)
                    print(f"[TRACE]   -> 结果：{reason2}。")
                    print(f"[TRACE]   --------------------------------------------------")
                else:
                    _print_singleline(ms, reason2)
                if ok2:
                    chosen_cnt += 1
                processed += 1

        print(f"\n[INFO] 通过筛选的市场数量：{chosen_cnt} / {len(mkts_raw)}")
        return

    # ---------- 非流式模式（批量） ----------
    market_list: List[MarketSnapshot] = []
    early_rejects: List[Tuple[MarketSnapshot, str]] = []

    for raw in mkts_raw:
        title = (raw.get("question") or raw.get("title") or "")
        slug  = (raw.get("slug") or "")
        if only_pat and (only_pat not in title.lower() and only_pat not in slug.lower()):
            continue
        ms = _parse_market(raw)
        ok, reason = _early_filter_reason(ms, args.min_end_hours, args.legacy_end_days)
        if ok:
            market_list.append(ms)
        else:
            early_rejects.append((ms, reason))

    if not args.skip_orderbook and market_list and (not args.no_rest_backfill):
        _rest_books_backfill(market_list, batch_size=args.books_batch_size)

    chosen: List[MarketSnapshot] = []
    rejects: List[Tuple[MarketSnapshot, str]] = early_rejects.copy()
    for ms in market_list:
        ok, reason = _final_pass_reason(ms, require_quotes=(not args.allow_illiquid))
        if ok:
            chosen.append(ms)
        else:
            rejects.append((ms, reason))

    if args.diagnose:
        shown = 0
        for i, (ms, reason) in enumerate(rejects[:args.diagnose_samples], start=1):
            _print_snapshot(i, len(rejects), ms)
            print(f"[TRACE]   -> 结果：{reason}。")
            print(f"[TRACE]   --------------------------------------------------")
            shown += 1
        if chosen:
            print("[INFO] （通过样本，最多显示 10 个）")
            for k, ms in enumerate(chosen[:10], start=1):
                yb = "-" if ms.yes.bid is None else f"{ms.yes.bid:.4f}"
                ya = "-" if ms.yes.ask is None else f"{ms.yes.ask:.4f}"
                nb = "-" if ms.no.bid is None else f"{ms.no.bid:.4f}"
                na = "-" if ms.no.ask is None else f"{ms.no.ask:.4f}"
                print(f"  [{k}] {ms.slug} | YES bid/ask={yb}/{ya} | NO bid/ask={nb}/{na} | LQ={_fmt_money(ms.liquidity)} Vol={_fmt_money(ms.totalVolume)}")

    print("")
    print(f"[INFO] 通过筛选的市场数量：{len(chosen)} / {len(mkts_raw)}")

if __name__ == "__main__":
    main()
