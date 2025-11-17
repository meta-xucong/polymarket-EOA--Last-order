# Volatility_arbitrage_price_watch_EOA.py
# -*- coding: utf-8 -*-
"""
基于 EOA 交易脚本的行情辅助模块。功能与 Safe 版一致，仅更新引用的 WS 连接器。
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

try:
    import requests
except Exception:
    requests = None

GAMMA_API = "https://gamma-api.polymarket.com/markets"

MODULE_VERSION = "2025-11-02"
_version_logged = False


def _log_version_once():
    global _version_logged
    if not _version_logged:
        print(f"[VER] Volatility_arbitrage_price_watch_EOA.py v{MODULE_VERSION}")
        _version_logged = True


def _is_url(s: str) -> bool:
    return s.startswith("http")


def _extract_market_slug(url: str) -> Optional[str]:
    m = re.search(r"/market/([^/?#]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/event/([^/?#]+)", url)
    return m.group(1) if m else None


def _gamma_fetch_market_by_slug(slug: str) -> Optional[dict]:
    if requests is None:
        print("[ERROR] 依赖 requests，请先安装： pip install requests")
        return None
    try:
        r = requests.get(GAMMA_API, params={"limit": 1, "slug": slug}, timeout=10)
        r.raise_for_status()
        arr = r.json()
        if isinstance(arr, list) and arr:
            return arr[0]
    except Exception as exc:
        print(f"[WARN] gamma-api 查询失败: {exc}")
    return None


def resolve_token_ids(source: str) -> Tuple[Optional[str], Optional[str], str, Optional[dict]]:
    if _is_url(source):
        slug = _extract_market_slug(source)
        if not slug:
            raise ValueError("无法从 URL 解析出 market/event slug")
        m = _gamma_fetch_market_by_slug(slug)
        if not m:
            raise ValueError(f"gamma-api 未找到该市场（slug={slug})")
        token_ids_raw = m.get("clobTokenIds", "[]")
        token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else (token_ids_raw or [])
        yes_id = token_ids[0] if len(token_ids) > 0 else None
        no_id = token_ids[1] if len(token_ids) > 1 else None
        title = m.get("question") or slug
        return yes_id, no_id, title, m

    if "," in source:
        a, b = source.split(",", 1)
        return (a.strip() or None), (b.strip() or None), "manual-token-ids", None

    raise ValueError("未识别的输入。请传入 Polymarket 市场 URL，或 'YES_id,NO_id'。")


# ============ 监听 & 节流输出 ============

def watch_prices(source: str, interval: int = 1) -> None:
    yes_id, no_id, label, _ = resolve_token_ids(source)
    asset_ids = [x for x in (yes_id, no_id) if x]

    from Volatility_arbitrage_main_ws_EOA import ws_watch_by_ids

    print(f"[INIT] 数据源: {label}")
    print(f"[INIT] YES token_id = {yes_id}")
    print(f"[INIT] NO  token_id = {no_id}")
    print(f"[RUN] 每 {interval}s 输出一次：YES/NO 买/卖（bid/ask），含最近成交价 price。Ctrl+C 结束。")

    latest: Dict[str, Dict[str, Any]] = {aid: {} for aid in asset_ids}

    def _parse_price_change(pc: Dict[str, Any]) -> Dict[str, Optional[float]]:
        def _to_float(val: Any) -> Optional[float]:
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        price_fields = (
            "last_trade_price",
            "last_price",
            "mark_price",
            "price",
        )
        best_bid_fields = ("best_bid", "bid")
        best_ask_fields = ("best_ask", "ask")

        price_val: Optional[float] = None
        for key in price_fields:
            price_val = _to_float(pc.get(key))
            if price_val is not None:
                break
        if price_val is None:
            bid_val = _to_float(pc.get("best_bid"))
            ask_val = _to_float(pc.get("best_ask"))
            if bid_val is not None and ask_val is not None:
                price_val = (bid_val + ask_val) / 2.0
            elif bid_val is not None:
                price_val = bid_val
            elif ask_val is not None:
                price_val = ask_val

        best_bid_val: Optional[float] = None
        for key in best_bid_fields:
            best_bid_val = _to_float(pc.get(key))
            if best_bid_val is not None:
                break

        best_ask_val: Optional[float] = None
        for key in best_ask_fields:
            best_ask_val = _to_float(pc.get(key))
            if best_ask_val is not None:
                break

        return {
            "price": price_val,
            "best_bid": best_bid_val,
            "best_ask": best_ask_val,
        }

    def _on_event(ev: Dict[str, Any]):
        if not isinstance(ev, dict):
            return
        if ev.get("event_type") == "price_change":
            pcs = ev.get("price_changes", [])
        elif "price_changes" in ev:
            pcs = ev.get("price_changes", [])
        else:
            return

        for pc in pcs:
            if not isinstance(pc, dict):
                continue
            token_id = pc.get("token_id") or pc.get("tokenId") or pc.get("id")
            if token_id not in latest:
                continue
            latest[token_id].update(_parse_price_change(pc))
            latest[token_id]["timestamp"] = time.time()

    thread = threading.Thread(
        target=ws_watch_by_ids,
        kwargs={"asset_ids": asset_ids, "label": label, "on_event": _on_event, "verbose": False},
        daemon=True,
    )
    thread.start()

    _log_version_once()

    try:
        while True:
            time.sleep(interval)
            now = datetime.now().strftime("%H:%M:%S")
            rows = []
            for aid in asset_ids:
                snap = latest.get(aid) or {}
                rows.append(
                    f"{aid}: bid={snap.get('best_bid')} ask={snap.get('best_ask')} last={snap.get('price')}"
                )
            print(f"[{now}] " + " | ".join(rows))
    except KeyboardInterrupt:
        print("[STOP] 用户终止。")


__all__ = ["resolve_token_ids", "watch_prices"]
