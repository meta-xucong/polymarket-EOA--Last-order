#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调试 Polymarket 市场最小下单量的辅助脚本。

用法示例：
    python test.py --slug amzn-up-or-down-on-november-12-2025 --side no
    python test.py --token-id 16314264174263237340163979877319935958603084504085913558466386842898436407639

脚本会调用现有的 EOA 客户端初始化逻辑，并依次尝试所有已知的
order book / market 接口来提取 documented ``minimum_order_size`` 字段，
输出每一步的命中情况，方便快速定位问题。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple

from collections.abc import Mapping as MappingABC

try:
    from Volatility_arbitrage_price_watch_EOA import _gamma_fetch_market_by_slug, resolve_token_ids
except Exception:
    _gamma_fetch_market_by_slug = None  # type: ignore
    resolve_token_ids = None  # type: ignore

from Volatility_arbitrage_main_rest_EOA import get_client
from Volatility_buy_EOA import (
    _extract_min_order_size_from_book,
    _extract_min_order_size_from_market,
)


def _iter_candidate_containers(client: Any) -> Iterator[Any]:
    """Yield all relevant nested containers on the Polymarket client."""

    yield client
    nested_attrs = (
        "public",
        "private",
        "market",
        "markets",
        "rest",
        "api",
        "client",
    )
    for attr in nested_attrs:
        try:
            nested = getattr(client, attr)
        except Exception:
            continue
        if nested is None:
            continue
        yield nested


def _normalize_payload(resp: Any) -> Any:
    payload = resp[1] if isinstance(resp, tuple) and len(resp) == 2 else resp
    if isinstance(payload, MappingABC) and "data" in payload and "status" in payload:
        payload = payload.get("data")
    return payload


def _invoke_method_with_trace(client: Any, name: str, kwargs: Mapping[str, Any]) -> Tuple[Optional[Any], str]:
    """Invoke a method on the client and nested containers, with trace output."""

    seen_ids = set()
    for container in _iter_candidate_containers(client):
        try:
            fn = getattr(container, name)
        except Exception:
            continue
        if not callable(fn):
            continue
        fn_id = id(fn)
        if fn_id in seen_ids:
            continue
        seen_ids.add(fn_id)
        container_name = getattr(container, "__class__", type(container)).__name__
        try:
            resp = fn(**kwargs)
        except TypeError:
            print(f"[TRACE] {container_name}.{name}{dict(kwargs)} -> TypeError，跳过。")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[TRACE] {container_name}.{name}{dict(kwargs)} -> 调用失败: {exc}")
            continue
        print(f"[TRACE] {container_name}.{name}{dict(kwargs)} -> 返回成功。")
        return resp, container_name
    return None, ""


BOOK_METHOD_CANDIDATES: Sequence[Tuple[str, Mapping[str, Any]]] = (
    ("get_order_book", {"token_id": "__TOKEN__"}),
    ("get_order_book", {"market": "__TOKEN__"}),
    ("get_order_book", {"market_id": "__TOKEN__"}),
    ("get_market_orderbook", {"token_id": "__TOKEN__"}),
    ("get_market_orderbook", {"market": "__TOKEN__"}),
    ("get_market_orderbook", {"market_id": "__TOKEN__"}),
    ("get_market_order_book", {"token_id": "__TOKEN__"}),
    ("get_market_order_book", {"market": "__TOKEN__"}),
    ("get_market_order_book", {"market_id": "__TOKEN__"}),
    ("get_orderbook", {"token_id": "__TOKEN__"}),
    ("get_orderbook", {"market": "__TOKEN__"}),
    ("get_orderbook", {"market_id": "__TOKEN__"}),
    ("order_book", {"token_id": "__TOKEN__"}),
    ("order_book", {"market": "__TOKEN__"}),
    ("order_book", {"market_id": "__TOKEN__"}),
    ("get_order_book_summary", {"token_id": "__TOKEN__"}),
    ("get_order_book_summary", {"market": "__TOKEN__"}),
    ("get_order_book_summary", {"market_id": "__TOKEN__"}),
)

BOOKS_METHOD_CANDIDATES: Sequence[Tuple[str, Mapping[str, Any]]] = (
    ("get_order_books", {"token_ids": ["__TOKEN__"]}),
    ("get_order_books", {"markets": ["__TOKEN__"]}),
    ("get_order_books", {"market_ids": ["__TOKEN__"]}),
    ("get_market_orderbooks", {"token_ids": ["__TOKEN__"]}),
    ("get_market_orderbooks", {"markets": ["__TOKEN__"]}),
    ("get_market_orderbooks", {"market_ids": ["__TOKEN__"]}),
    ("get_market_order_books", {"token_ids": ["__TOKEN__"]}),
    ("get_market_order_books", {"markets": ["__TOKEN__"]}),
    ("get_market_order_books", {"market_ids": ["__TOKEN__"]}),
)

MARKETS_METHOD_CANDIDATES: Sequence[Tuple[str, Mapping[str, Any]]] = (
    ("get_markets", {"ids": ["__TOKEN__"]}),
    ("get_markets", {"token_ids": ["__TOKEN__"]}),
    ("get_markets", {"market_ids": ["__TOKEN__"]}),
    ("list_markets", {"ids": ["__TOKEN__"]}),
    ("list_markets", {"token_ids": ["__TOKEN__"]}),
    ("list_markets", {"market_ids": ["__TOKEN__"]}),
    ("markets", {"ids": ["__TOKEN__"]}),
    ("markets", {"market_ids": ["__TOKEN__"]}),
)

MARKET_METHOD_CANDIDATES: Sequence[Tuple[str, Mapping[str, Any]]] = (
    ("get_market", {"token_id": "__TOKEN__"}),
    ("get_market", {"market": "__TOKEN__"}),
    ("get_market", {"market_id": "__TOKEN__"}),
    ("market", {"token_id": "__TOKEN__"}),
    ("market", {"market": "__TOKEN__"}),
    ("market", {"market_id": "__TOKEN__"}),
)


def _substitute_token(params: Mapping[str, Any], token_id: str) -> Dict[str, Any]:
    return {
        key: (
            [token_id if item == "__TOKEN__" else item for item in value]
            if isinstance(value, list)
            else (token_id if value == "__TOKEN__" else value)
        )
        for key, value in params.items()
    }


def debug_fetch_market_min_order_size(client: Any, token_id: str) -> Optional[float]:
    print(f"[INFO] 开始查询 token_id={token_id} 的最小下单量。")

    def _attempt(candidates: Sequence[Tuple[str, Mapping[str, Any]]], extractor) -> Optional[float]:
        for name, params in candidates:
            kwargs = _substitute_token(params, token_id)
            resp, container_name = _invoke_method_with_trace(client, name, kwargs)
            if resp is None:
                continue
            payload = _normalize_payload(resp)
            try:
                extracted = extractor(payload)
            except Exception as exc:  # noqa: BLE001
                print(f"[TRACE] {container_name}.{name} -> 解析失败: {exc}")
                continue
            print(
                f"[TRACE] {container_name}.{name} -> 提取结果: {extracted!r}"
            )
            if extracted is not None and extracted > 0:
                print(f"[INFO] 通过 {container_name}.{name} 成功取得最小下单量: {extracted}")
                return float(extracted)
        return None

    result = _attempt(BOOK_METHOD_CANDIDATES, _extract_min_order_size_from_book)
    if result is not None:
        return result

    result = _attempt(BOOKS_METHOD_CANDIDATES, _extract_min_order_size_from_book)
    if result is not None:
        return result

    result = _attempt(MARKETS_METHOD_CANDIDATES, _extract_min_order_size_from_market)
    if result is not None:
        return result

    result = _attempt(MARKET_METHOD_CANDIDATES, _extract_min_order_size_from_market)
    if result is not None:
        return result

    print("[WARN] 所有候选接口均未返回有效的最小下单量。")
    return None


def _parse_token_ids_from_meta(meta: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    token_ids_raw = meta.get("clobTokenIds")
    token_ids: Sequence[Any]
    if isinstance(token_ids_raw, str):
        try:
            token_ids = json.loads(token_ids_raw)
        except Exception:
            token_ids = []
    elif isinstance(token_ids_raw, (list, tuple)):
        token_ids = list(token_ids_raw)
    else:
        token_ids = []
    yes_id = token_ids[0] if len(token_ids) > 0 else None
    no_id = token_ids[1] if len(token_ids) > 1 else None
    return (yes_id, no_id)


def resolve_token(identifier: str, side: str) -> Tuple[str, Dict[str, Any]]:
    identifier = identifier.strip()
    if not identifier:
        raise ValueError("必须提供有效的 token_id 或 slug/url。")

    if identifier.isdigit():
        return identifier, {"source": "token", "label": identifier}

    # 兼容直接输入 YES_id,NO_id
    if "," in identifier:
        yes_id, no_id = [part.strip() or None for part in identifier.split(",", 1)]
        token = yes_id if side == "yes" else no_id
        if not token:
            raise ValueError("输入未包含所需方向的 token_id。")
        return token, {"source": "pair", "label": identifier, "yes": yes_id, "no": no_id}

    # URL 交给现有的解析器
    if identifier.startswith("http") and resolve_token_ids:
        yes_id, no_id, title, meta = resolve_token_ids(identifier)
        token = yes_id if side == "yes" else no_id
        if not token:
            raise ValueError("解析 URL 失败，未找到对应方向的 token_id。")
        return token, {"source": "url", "label": title, "yes": yes_id, "no": no_id, "meta": meta}

    # slug 使用 gamma-api 查询
    if _gamma_fetch_market_by_slug is None:
        raise RuntimeError("缺少 requests 依赖，无法根据 slug 查询市场。")
    meta = _gamma_fetch_market_by_slug(identifier)
    if not meta:
        raise ValueError(f"gamma-api 未找到该市场（slug={identifier}）。")
    yes_id, no_id = _parse_token_ids_from_meta(meta)
    token = yes_id if side == "yes" else no_id
    if not token:
        raise ValueError("市场元数据缺少所需方向的 token_id。")
    title = meta.get("question") or meta.get("title") or identifier
    return token, {"source": "slug", "label": title, "slug": identifier, "yes": yes_id, "no": no_id, "meta": meta}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="测试市场最小下单量提取逻辑。")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--token-id", dest="token_id", help="直接指定 token_id。")
    group.add_argument("--slug", dest="slug", help="Polymarket 市场 slug，例如 'amzn-up-or-down-on-november-12-2025'。")
    group.add_argument("--source", dest="source", help="市场 URL 或 'YES_id,NO_id' 组合。")
    parser.add_argument("--side", choices=("yes", "no"), default="no", help="当输入为 slug/url 时，指定需要查询的方向。默认 NO。")
    args = parser.parse_args(argv)

    identifier = args.token_id or args.slug or args.source
    side = args.side

    try:
        token_id, info = resolve_token(identifier, side)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 解析 token_id 失败: {exc}")
        return 1

    print(f"[INFO] 目标市场: {info.get('label')} | side={side.upper()} | token_id={token_id}")
    if "slug" in info:
        print(f"[INFO] slug={info['slug']}")
    if info.get("yes") or info.get("no"):
        print(f"[INFO] YES token_id={info.get('yes')} | NO token_id={info.get('no')}")

    client = get_client()

    result = debug_fetch_market_min_order_size(client, token_id)
    if result is None:
        print("[FAIL] 未能获得有效的最小下单量。")
        return 2

    print(f"[SUCCESS] 最小下单量={result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
