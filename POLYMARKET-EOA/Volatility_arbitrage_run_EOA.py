#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Volatility_arbitrage_run_EOA.py  ·  买入流程入口

新版入口仅负责三件事：
1. 启动时询问买入份数（留空则按市场最小下单量执行）；
2. 调用 Volatility_fliter_EOA 运行最新筛选，获取满足严格条件的市场/方向；
3. 针对命中的 token 逐一执行买单，不再包含价格监控、卖出或自动 claim 逻辑。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional, Tuple

from Volatility_fliter_EOA import (
    HighlightedOutcome,
    collect_filter_results,
)
from Volatility_buy_EOA import execute_auto_buy
from trading.execution import ExecutionResult


# --------------------------------------
# 基础工具
# --------------------------------------

def _get_client():
    try:
        from Volatility_arbitrage_main_ws_EOA import get_client  # 优先使用 WS 版

        return get_client()
    except Exception as exc_ws:  # pragma: no cover - 仅在运行时展示
        try:
            from Volatility_arbitrage_main_rest_EOA import (
                get_client as get_rest_client,
            )

            return get_rest_client()
        except Exception as exc_rest:  # pragma: no cover - 仅在运行时展示
            print(f"[ERR] 无法导入 get_client：{exc_ws} | {exc_rest}")
            sys.exit(1)


def _coerce_positive_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            raw = value.replace(",", "").strip()
            if not raw:
                return None
            numeric = float(raw)
        else:
            numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


def _infer_min_order_size(highlight: HighlightedOutcome) -> Optional[float]:
    raw = highlight.market.raw or {}
    for key in (
        "minimumOrderSize",
        "minimum_order_size",
        "minOrderSize",
        "min_order_size",
    ):
        val = _coerce_positive_float(raw.get(key))
        if val is not None:
            return val
    return None


def _infer_tick_size(highlight: HighlightedOutcome) -> Optional[float]:
    raw = highlight.market.raw or {}
    for key in (
        "minimumTickSize",
        "minimum_tick_size",
        "minTickSize",
        "min_tick_size",
        "tickSize",
    ):
        val = _coerce_positive_float(raw.get(key))
        if val is not None:
            return val
    return None


def _extract_api_creds(client) -> Optional[Dict[str, str]]:
    def _pair_from_mapping(mp: Dict[str, Any]) -> Optional[Dict[str, str]]:
        if not isinstance(mp, dict):
            return None
        key_keys = ("key", "apiKey", "api_key", "id", "apiId", "api_id")
        secret_keys = ("secret", "apiSecret", "api_secret", "apiSecretKey")
        key_val = next((mp.get(k) for k in key_keys if mp.get(k)), None)
        secret_val = next((mp.get(k) for k in secret_keys if mp.get(k)), None)
        if key_val and secret_val:
            return {"key": str(key_val), "secret": str(secret_val)}
        return None

    def _pair_from_object(obj: Any) -> Optional[Dict[str, str]]:
        if obj is None:
            return None
        for attr_key in ("key", "apiKey", "api_key", "id", "apiId", "api_id"):
            key_val = getattr(obj, attr_key, None)
            if key_val:
                break
        else:
            key_val = None
        for attr_secret in ("secret", "apiSecret", "api_secret", "apiSecretKey"):
            secret_val = getattr(obj, attr_secret, None)
            if secret_val:
                break
        else:
            secret_val = None
        if key_val and secret_val:
            return {"key": str(key_val), "secret": str(secret_val)}
        if hasattr(obj, "to_dict"):
            try:
                return _pair_from_mapping(obj.to_dict())
            except Exception:
                return None
        return None

    def _pair_from_sequence(seq: Any) -> Optional[Dict[str, str]]:
        if not isinstance(seq, (list, tuple)) or len(seq) < 2:
            return None
        key_val, secret_val = seq[0], seq[1]
        if key_val and secret_val:
            return {"key": str(key_val), "secret": str(secret_val)}
        return None

    candidates = [
        getattr(client, "api_creds", None),
        getattr(client, "_api_creds", None),
    ]
    getter = getattr(client, "get_api_creds", None)
    if callable(getter):
        try:
            candidates.append(getter())
        except Exception:
            pass
    key = getattr(client, "api_key", None)
    secret = getattr(client, "api_secret", None)
    if key and secret:
        candidates.append({"key": key, "secret": secret})
    env_key = os.getenv("POLY_API_KEY")
    env_secret = os.getenv("POLY_API_SECRET")
    if env_key and env_secret:
        candidates.append({"key": env_key, "secret": env_secret})

    for cand in candidates:
        if cand is None:
            continue
        pair = None
        if isinstance(cand, dict):
            pair = _pair_from_mapping(cand)
        elif isinstance(cand, (list, tuple)):
            pair = _pair_from_sequence(cand)
        else:
            pair = _pair_from_object(cand)
        if pair:
            return pair
    return None


def _prompt_order_size() -> Optional[float]:
    prompt = "请输入买入份数（留空=使用市场最小下单量）："
    while True:
        try:
            raw = input(prompt)
        except EOFError:
            print("\n[INFO] 未提供输入，默认为市场最小下单量。")
            return None
        if raw is None:
            return None
        raw = raw.strip()
        if not raw:
            return None
        try:
            val = float(raw)
        except ValueError:
            print("[WARN] 无法解析输入，请输入正数或直接回车。")
            continue
        if val <= 0:
            print("[WARN] 买入份数必须为正数，请重新输入。")
            continue
        return float(val)


def _format_highlight(ho: HighlightedOutcome, index: int) -> str:
    ms = ho.market
    snap = ho.outcome
    ask = f"{snap.ask:.4f}" if snap.ask is not None else "-"
    bid = f"{snap.bid:.4f}" if snap.bid is not None else "-"
    hours = f"{ho.hours_to_end:.1f}" if ho.hours_to_end is not None else "-"
    return (
        f"  [{index}] slug={ms.slug} | 标题={ms.title} | 方向={snap.name}"
        f" | token_id={snap.token_id or '-'} | bid/ask={bid}/{ask} | ends_in={hours}h"
    )


def _resolve_order_size(
    desired: Optional[float],
    market_min: Optional[float],
) -> Tuple[float, Optional[str]]:
    if desired is not None:
        if market_min is not None and desired + 1e-9 < market_min:
            return market_min, (
                f"[WARN] 输入份数 {desired} 低于市场最小下单量 {market_min}，自动上调至最小值。"
            )
        return desired, None
    if market_min is not None:
        return market_min, f"[INFO] 使用市场最小下单量 {market_min}。"
    return 1.0, "[INFO] 市场未给出最小下单量，默认下单 1 份。"


def _print_execution_result(resp: ExecutionResult) -> None:
    status = resp.status if resp.status else "UNKNOWN"
    filled = getattr(resp, "filled", 0.0) or 0.0
    requested = getattr(resp, "requested", 0.0) or 0.0
    avg_price = resp.avg_price if resp.avg_price is not None else "-"
    last_price = resp.last_price if resp.last_price is not None else "-"
    message = resp.message or "-"
    print(
        f"    -> status={status} | filled={filled:.4f}/{requested:.4f}"
        f" | avg_price={avg_price} | last_price={last_price} | message={message}"
    )
    remaining = resp.remaining if hasattr(resp, "remaining") else 0.0
    if remaining and remaining > 1e-9:
        print(f"    -> 未成交数量：{remaining:.4f}")


def main() -> None:
    desired_size = _prompt_order_size()
    if desired_size is not None:
        print(f"[INFO] 将按输入份数 {desired_size} 执行买单。")
    else:
        print("[INFO] 未输入买入份数，将按市场最小下单量或默认 1 份执行。")

    print("[STEP] 正在运行市场筛选器…")
    result = collect_filter_results()
    highlights = list(result.highlights)
    print(
        f"[INFO] 筛选完成：候选 {len(result.chosen)} / 总计 {result.total_markets}，"
        f"满足严格条件 {len(highlights)} 项。"
    )

    if not highlights:
        print("[INFO] 当前无满足条件的市场，流程结束。")
        print("[INFO] 本脚本不包含自动 claim，请在结算后于官网手动操作。")
        return

    print("[INFO] 命中列表：")
    for idx, ho in enumerate(highlights, start=1):
        print(_format_highlight(ho, idx))

    print("[STEP] 初始化 CLOB 客户端…")
    client = _get_client()
    creds = _extract_api_creds(client)
    if not creds or not creds.get("key") or not creds.get("secret"):
        print("[ERR] 无法获取完整的 API 凭证，请检查配置后重试。")
        return
    key_preview = creds["key"]
    if len(key_preview) > 10:
        key_preview = f"{key_preview[:6]}***{key_preview[-4:]}"
    print(f"[INIT] API Key：{key_preview}")

    for idx, ho in enumerate(highlights, start=1):
        snap = ho.outcome
        token_id = snap.token_id
        ask_price = snap.ask
        if not token_id or ask_price is None:
            print(f"[SKIP] #{idx} 缺少 token_id 或卖价，跳过。")
            continue

        market_min = _infer_min_order_size(ho)
        tick_size = _infer_tick_size(ho)
        order_size, note = _resolve_order_size(desired_size, market_min)
        if note:
            print(note)

        print(
            f"[BUY] #{idx} slug={ho.market.slug} | 方向={snap.name}"
            f" | token_id={token_id} | ask={ask_price:.4f} | size={order_size:.4f}"
        )
        try:
            resp = execute_auto_buy(
                client=client,
                token_id=str(token_id),
                price=float(ask_price),
                size=float(order_size),
                min_order_size=float(market_min) if market_min else 0.0,
                tick_size=float(tick_size) if tick_size else 0.0,
            )
        except Exception as exc:  # pragma: no cover - 网络/SDK 异常
            print(f"    -> [ERR] 下单失败：{exc}")
            continue

        if isinstance(resp, ExecutionResult):
            _print_execution_result(resp)
        else:
            print(f"    -> [INFO] 下单响应：{resp}")

    print("[DONE] 买单流程结束，后续请等待市场结算并手动 claim。")


if __name__ == "__main__":
    main()
