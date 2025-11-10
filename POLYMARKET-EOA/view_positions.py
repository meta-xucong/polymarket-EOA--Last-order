#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速查看当前 Polymarket EOA 钱包的持仓概览。

运行方式：

    $ python view_positions.py

脚本会尽可能重用仓库中现有的 EOA 客户端与工具函数，自动读取
环境变量中配置的钱包与 API 凭据，优先通过 ``py_clob_client`` 提供的
客户端获取余额/持仓；若客户端未暴露相关接口，则回落到公开 REST
接口抓取数据。

输出内容包括：
- 当前 USDC 可用余额（若可获取）；
- 每个 token 的持仓数量、均价、标记价格、名义价值、成本与未实现盈亏；
- 聚合统计（YES/NO/全部持仓的总数量与价值）。

若检测到网络或接口异常，脚本会给出清晰的诊断信息，方便用户排查。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # ``requests`` 已是现有依赖，若缺失则给出明确提示
    import requests
except Exception as exc:  # pragma: no cover - 运行时兜底
    print("[FATAL] 缺少依赖：requests。请先执行 `pip install requests`", file=sys.stderr)
    raise


# -------------------------------
# 复用仓库内已有工具
# -------------------------------

try:
    from Volatility_arbitrage_main_rest_EOA import (
        get_client as _get_eoa_client,
    )
except Exception as exc:  # pragma: no cover - 运行时兜底
    _get_eoa_client = None  # type: ignore[assignment]
    _IMPORT_CLIENT_ERROR = exc
else:
    _IMPORT_CLIENT_ERROR = None

try:
    from Volatility_buy_EOA import _fetch_available_quote_balance as _fetch_quote_balance
except Exception:
    _fetch_quote_balance = None  # type: ignore[assignment]

try:
    from Volatility_fliter_EOA import build_market_url as _build_market_url  # type: ignore[attr-defined]
except Exception:
    _build_market_url = None  # type: ignore[assignment]


# -------------------------------
# 常量与工具函数
# -------------------------------

POLY_HOST = os.environ.get("POLY_HOST", "https://clob.polymarket.com").rstrip("/")
GAMMA_HOST = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com").rstrip("/")

_ADDR_ENV_CANDIDATES = [
    "POLY_EOA_ADDRESS",
    "POLY_ADDRESS",
    "POLY_WALLET",
    "POLYMARKET_WALLET",
    "POLY_SAFE_ADDRESS",  # 兼容旧变量（若用户沿用历史环境变量）
]

_KEY_ENV_CANDIDATES = [
    "POLY_EOA_KEY",
    "POLY_KEY",
    "POLY_PRIVATE_KEY",
    "PRIVATE_KEY",
]


def _normalize_address(raw: str) -> Optional[str]:
    if not raw:
        return None
    addr = raw.strip()
    if not addr:
        return None
    if addr.startswith(("0x", "0X")):
        addr = "0x" + addr[2:].lower()
    else:
        addr = "0x" + addr.lower() if len(addr) == 40 else addr.lower()
    if len(addr) != 42:
        return None
    return addr


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        txt = value.strip().replace(",", "")
        if not txt:
            return None
        try:
            return float(txt)
        except ValueError:
            return None
    return None


def _ensure_mapping(obj: Any) -> Mapping[str, Any]:
    if isinstance(obj, Mapping):
        return obj
    if hasattr(obj, "to_dict"):
        try:
            mp = obj.to_dict()
        except Exception:
            mp = None
        if isinstance(mp, Mapping):
            return mp
    if hasattr(obj, "_asdict"):
        try:
            mp = obj._asdict()  # type: ignore[attr-defined]
        except Exception:
            mp = None
        if isinstance(mp, Mapping):
            return mp
    if hasattr(obj, "__dict__"):
        mp = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
        if mp:
            return mp
    if isinstance(obj, Sequence) and len(obj) >= 2:
        # 尝试把序列解析成 (token_id, amount, ...)
        return {"token_id": obj[0], "amount": obj[1]}
    return {}


def _maybe_get(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    for key in keys:
        if hasattr(mapping, key):
            try:
                val = getattr(mapping, key)
            except Exception:
                continue
            if val not in (None, ""):
                return val
    return None


def _merge_dicts(a: Mapping[str, Any], b: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(a)
    for k, v in b.items():
        if k not in merged or merged[k] in (None, ""):
            merged[k] = v
    return merged


def _derive_address_from_key(raw_key: str) -> Optional[str]:
    try:
        from eth_account import Account  # type: ignore
    except Exception:
        return None

    key = raw_key.strip()
    if not key:
        return None
    if not key.startswith("0x"):
        key = "0x" + key
    try:
        account = Account.from_key(key)
    except Exception:
        return None
    address = getattr(account, "address", None)
    if isinstance(address, str):
        return _normalize_address(address)
    return None


# -------------------------------
# 数据结构
# -------------------------------


@dataclass
class PositionRecord:
    token_id: str
    side: str = ""
    outcome: str = ""
    quantity: float = 0.0
    avg_price: Optional[float] = None
    mark_price: Optional[float] = None
    pnl: Optional[float] = None
    cost: Optional[float] = None
    mark_value: Optional[float] = None
    market_question: Optional[str] = None
    market_slug: Optional[str] = None
    market_id: Optional[str] = None
    event_slug: Optional[str] = None
    event_title: Optional[str] = None
    last_trade_price: Optional[float] = None
    last_update: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def sync_totals(self) -> None:
        if self.avg_price is not None:
            self.cost = self.quantity * self.avg_price
        if self.mark_price is not None:
            self.mark_value = self.quantity * self.mark_price
        if self.cost is not None and self.mark_value is not None:
            self.pnl = self.mark_value - self.cost

    @property
    def direction(self) -> str:
        if self.side:
            return self.side.upper()
        if self.outcome:
            return self.outcome.upper()
        return ""

    @property
    def market_url(self) -> Optional[str]:
        slug = self.market_slug or ""
        if not slug:
            return None
        if _build_market_url and self.raw.get("market"):
            try:
                return _build_market_url(self.raw["market"])
            except Exception:
                pass
        if self.event_slug:
            return f"https://polymarket.com/event/{self.event_slug}/{slug}"
        return f"https://polymarket.com/market/{slug}"


# -------------------------------
# 钱包地址与余额探测
# -------------------------------


def _infer_wallet_address(client: Any = None) -> Optional[str]:
    for env_name in _ADDR_ENV_CANDIDATES:
        addr = _normalize_address(os.getenv(env_name, ""))
        if addr:
            return addr

    attr_candidates = [
        "funder",
        "wallet_address",
        "walletAddress",
        "account_address",
        "accountAddress",
        "address",
        "user_address",
        "userAddress",
        "owner",
        "trader_address",
    ]
    for name in attr_candidates:
        if client is None:
            break
        try:
            value = getattr(client, name)
        except Exception:
            continue
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        addr = _normalize_address(str(value)) if value else None
        if addr:
            return addr

    for env_name in _KEY_ENV_CANDIDATES:
        raw_key = os.getenv(env_name)
        if raw_key:
            derived = _derive_address_from_key(raw_key)
            if derived:
                return derived

    return None


def _safe_fetch_quote_balance(client: Any) -> Optional[float]:
    if client is None or _fetch_quote_balance is None:
        return None
    try:
        return _fetch_quote_balance(client)
    except Exception:
        return None


# -------------------------------
# 持仓解析
# -------------------------------


def _unwrap_payload(payload: Any) -> Any:
    if payload is None:
        return None
    if isinstance(payload, tuple) and len(payload) == 2:
        # ``py_clob_client`` 多数接口返回 (status, data)
        return payload[1]
    if isinstance(payload, Mapping):
        lower_keys = {k.lower(): k for k in payload.keys()}
        for key in ("data", "result", "payload", "response", "positions"):
            lk = key.lower()
            if lk in lower_keys:
                return payload[lower_keys[lk]]
        return payload
    return payload


def _as_iterable(obj: Any) -> Iterable[Any]:
    if obj is None:
        return []
    if isinstance(obj, (list, tuple, set)):
        return obj
    if isinstance(obj, Mapping):
        # 有些接口返回 {token_id: {...}}
        return obj.values()
    return [obj]


def _extract_position_core(item: Mapping[str, Any]) -> Optional[PositionRecord]:
    merged = dict(item)

    # 嵌套结构：尝试合并 "token" / "market" / "outcome" 字段
    nested_keys = ["token", "asset", "position", "positionToken", "holding", "node"]
    for key in nested_keys:
        nested = item.get(key)
        if isinstance(nested, Mapping):
            merged = _merge_dicts(merged, nested)
        elif hasattr(nested, "to_dict"):
            try:
                sub = nested.to_dict()
            except Exception:
                sub = None
            if isinstance(sub, Mapping):
                merged = _merge_dicts(merged, sub)

    token_id = _maybe_get(merged, "token_id", "tokenId", "asset_id", "assetId", "id", "token")
    if not token_id:
        return None

    token_id = str(token_id)
    qty = _coerce_float(
        _maybe_get(
            merged,
            "amount",
            "quantity",
            "qty",
            "size",
            "shares",
            "position",
            "balance",
            "num_shares",
            "number",
        )
    )
    if qty is None:
        qty = 0.0

    avg_price = _coerce_float(_maybe_get(merged, "avg_price", "average_price", "entry_price", "avgPrice"))
    mark_price = _coerce_float(
        _maybe_get(
            merged,
            "mark_price",
            "price",
            "last_price",
            "lastPrice",
            "markPrice",
            "current_price",
            "price_average",
        )
    )

    direction = _maybe_get(merged, "side", "outcome", "direction", "position_type", "type")
    outcome = _maybe_get(merged, "outcome", "name", "label", "title")
    market_question = _maybe_get(merged, "question", "market_question", "marketQuestion", "marketTitle")
    market_slug = _maybe_get(merged, "slug", "market_slug", "marketSlug")
    market_id = _maybe_get(merged, "market_id", "marketId", "condition_id", "conditionId")
    event_slug = _maybe_get(merged, "event_slug", "eventSlug")
    event_title = _maybe_get(merged, "event_title", "eventTitle")
    last_price = _coerce_float(_maybe_get(merged, "last_price", "lastPrice"))
    last_update = _maybe_get(merged, "updated_at", "updatedAt", "timestamp", "lastUpdate")

    record = PositionRecord(
        token_id=token_id,
        side=str(direction or outcome or ""),
        outcome=str(outcome or direction or ""),
        quantity=float(qty),
        avg_price=avg_price,
        mark_price=mark_price if mark_price is not None else last_price,
        market_question=(str(market_question) if market_question else None),
        market_slug=(str(market_slug) if market_slug else None),
        market_id=str(market_id) if market_id else None,
        event_slug=str(event_slug) if event_slug else None,
        event_title=str(event_title) if event_title else None,
        last_trade_price=last_price,
        last_update=str(last_update) if last_update else None,
        raw=dict(item),
    )
    record.sync_totals()
    return record


def _extract_positions(payload: Any) -> List[PositionRecord]:
    payload = _unwrap_payload(payload)
    if payload is None:
        return []

    if isinstance(payload, Mapping):
        for key in (
            "positions",
            "data",
            "result",
            "portfolio",
            "holdings",
            "items",
            "balances",
            "outcomes",
            "positionBalances",
            "tokenPositions",
        ):
            if key in payload:
                nested = payload[key]
                return _extract_positions(nested)
        # 有些接口以 tokenId 为 key
        records = []
        for value in payload.values():
            if isinstance(value, (Mapping, list)):
                records.extend(_extract_positions(value))
        if records:
            return records
        payload_list = [payload]
    else:
        payload_list = list(_as_iterable(payload))

    results: List[PositionRecord] = []
    for item in payload_list:
        mapping = _ensure_mapping(item)
        if not mapping:
            continue
        record = _extract_position_core(mapping)
        if record:
            results.append(record)
    return results


# -------------------------------
# 持仓数据抓取
# -------------------------------


def _fetch_positions_from_client(client: Any, wallet: Optional[str]) -> Tuple[List[PositionRecord], List[str]]:
    if client is None:
        return [], []

    errors: List[str] = []
    results: List[PositionRecord] = []

    def try_call(name: str, kwargs: Optional[Dict[str, Any]] = None) -> Optional[List[PositionRecord]]:
        fn = getattr(client, name, None)
        if not callable(fn):
            return None
        call_kwargs = dict(kwargs or {})
        try:
            resp = fn(**call_kwargs)
        except TypeError:
            # 参数不匹配，忽略
            return None
        except Exception as exc:
            errors.append(f"{name} -> {exc}")
            return None
        extracted = _extract_positions(resp)
        if extracted:
            return extracted
        return []

    # 尝试常见方法
    method_candidates: List[Tuple[str, Optional[Dict[str, Any]]]] = [
        ("get_positions", None),
        ("get_user_positions", None),
        ("get_account_positions", None),
        ("get_trading_positions", None),
        ("get_positions_by_wallet", {"wallet_address": wallet} if wallet else None),
        ("get_positions", {"wallet": wallet} if wallet else None),
        ("get_user_portfolio", None),
        ("get_portfolio", None),
        ("get_holdings", None),
    ]

    for name, kwargs in method_candidates:
        if kwargs is None and "wallet" in (name or "") and wallet is None:
            continue
        res = try_call(name, kwargs)
        if res:
            results = res
            break

    return results, errors


def _fetch_positions_via_http(wallet: Optional[str]) -> Tuple[List[PositionRecord], List[str]]:
    if not wallet:
        return [], ["无法推断钱包地址，HTTP 方式已跳过。"]

    headers = {
        "User-Agent": "Mozilla/5.0 (view_positions)",
        "Accept": "application/json",
    }

    attempts: List[Tuple[str, str, Dict[str, Any]]] = [
        ("GET", f"{POLY_HOST}/positions", {"wallet": wallet}),
        ("GET", f"{POLY_HOST}/positions/{wallet}", {}),
        ("GET", f"{POLY_HOST}/accounts/{wallet}/positions", {}),
        ("GET", f"{POLY_HOST}/balances/{wallet}", {}),
        ("GET", f"{GAMMA_HOST}/wallets/{wallet}/positions", {}),
        ("GET", f"{GAMMA_HOST}/wallets/{wallet}", {"withPositions": "true"}),
        ("GET", f"{GAMMA_HOST}/portfolio/{wallet}", {}),
        ("GET", f"{GAMMA_HOST}/users/{wallet}/positions", {}),
    ]

    errors: List[str] = []

    for method, url, params in attempts:
        try:
            if method == "GET":
                resp = requests.get(url, params=params or None, headers=headers, timeout=10)
            else:
                resp = requests.post(url, json=params or None, headers=headers, timeout=10)
        except Exception as exc:
            errors.append(f"{method} {url} -> {exc}")
            continue

        if resp.status_code >= 400:
            errors.append(f"{method} {url} -> HTTP {resp.status_code}")
            continue

        try:
            data = resp.json()
        except Exception as exc:
            errors.append(f"{method} {url} -> JSON 解析失败: {exc}")
            continue

        positions = _extract_positions(data)
        if positions:
            return positions, errors

    return [], errors


# -------------------------------
# 输出格式化
# -------------------------------


def _fmt_money(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "-"
    try:
        return f"{value:,.{digits}f}"
    except Exception:
        return str(value)


def _fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "-"
    try:
        return f"{value:.4f}"
    except Exception:
        return str(value)


def _fmt_quantity(value: float) -> str:
    if math.isclose(value, round(value)):
        return f"{round(value):,.0f}"
    return f"{value:,.4f}"


def _build_table(records: List[PositionRecord]) -> str:
    if not records:
        return "[INFO] 当前没有持仓。"

    headers = [
        "#",
        "市场/方向",
        "token_id",
        "数量",
        "均价",
        "现价",
        "成本",
        "名义价值",
        "未实现盈亏",
    ]

    rows: List[List[str]] = []
    for idx, rec in enumerate(records, start=1):
        market = rec.market_question or rec.market_slug or "(未知市场)"
        direction = rec.direction or "?"
        title = f"{market} | {direction}"
        rows.append(
            [
                str(idx),
                title,
                rec.token_id,
                _fmt_quantity(rec.quantity),
                _fmt_price(rec.avg_price),
                _fmt_price(rec.mark_price),
                _fmt_money(rec.cost),
                _fmt_money(rec.mark_value),
                _fmt_money(rec.pnl),
            ]
        )

    # 计算列宽
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def format_row(row: List[str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    sep = "-+-".join("-" * w for w in widths)
    lines = [format_row(headers), sep]
    lines.extend(format_row(row) for row in rows)
    return "\n".join(lines)


def _aggregate_stats(records: List[PositionRecord]) -> Dict[str, Dict[str, float]]:
    buckets = {
        "ALL": {
            "qty": 0.0,
            "cost": 0.0,
            "value": 0.0,
            "pnl": 0.0,
        },
        "YES": {
            "qty": 0.0,
            "cost": 0.0,
            "value": 0.0,
            "pnl": 0.0,
        },
        "NO": {
            "qty": 0.0,
            "cost": 0.0,
            "value": 0.0,
            "pnl": 0.0,
        },
    }

    for rec in records:
        buckets["ALL"]["qty"] += rec.quantity
        if rec.cost is not None:
            buckets["ALL"]["cost"] += rec.cost
        if rec.mark_value is not None:
            buckets["ALL"]["value"] += rec.mark_value
        if rec.pnl is not None:
            buckets["ALL"]["pnl"] += rec.pnl

        direction = rec.direction
        if direction in buckets:
            buckets[direction]["qty"] += rec.quantity
            if rec.cost is not None:
                buckets[direction]["cost"] += rec.cost
            if rec.mark_value is not None:
                buckets[direction]["value"] += rec.mark_value
            if rec.pnl is not None:
                buckets[direction]["pnl"] += rec.pnl

    return buckets


def _print_summary(records: List[PositionRecord]) -> None:
    stats = _aggregate_stats(records)
    print("\n[SUMMARY] 持仓统计：")
    for key in ("ALL", "YES", "NO"):
        bucket = stats[key]
        qty = _fmt_quantity(bucket["qty"]) if bucket["qty"] else "0"
        cost = _fmt_money(bucket["cost"]) if bucket["cost"] else "0.00"
        value = _fmt_money(bucket["value"]) if bucket["value"] else "0.00"
        pnl = _fmt_money(bucket["pnl"]) if bucket["pnl"] else "0.00"
        label = {
            "ALL": "全部",
            "YES": "YES 合约",
            "NO": "NO 合约",
        }[key]
        print(f"  - {label:<6} | 数量={qty:<12} | 成本={cost:<14} | 名义价值={value:<14} | 未实现盈亏={pnl}")


# -------------------------------
# 主逻辑
# -------------------------------


def _load_client(verbose: bool = True):
    if _get_eoa_client is None:
        if verbose:
            print(f"[WARN] 无法导入 EOA 客户端：{_IMPORT_CLIENT_ERROR}")
        return None
    try:
        return _get_eoa_client()
    except Exception as exc:
        if verbose:
            print(f"[WARN] 初始化 ClobClient 失败：{exc}")
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="查看 Polymarket EOA 当前持仓")
    ap.add_argument("--json", action="store_true", help="以 JSON 格式输出持仓明细（适合集成到其他脚本）")
    ap.add_argument("--limit", type=int, default=0, help="仅显示前 N 条持仓")
    ap.add_argument("--verbose", action="store_true", help="输出额外的诊断信息")
    args = ap.parse_args(argv)

    client = _load_client(verbose=args.verbose)
    wallet = _infer_wallet_address(client)
    if wallet:
        print(f"[INFO] 使用钱包地址：{wallet}")
    else:
        print("[WARN] 未能推断钱包地址，将尝试仅依赖客户端接口。")

    quote_balance = _safe_fetch_quote_balance(client)
    if quote_balance is not None:
        print(f"[INFO] 当前可用 USDC 余额：{_fmt_money(quote_balance)}")

    positions: List[PositionRecord] = []
    errors: List[str] = []

    client_positions, client_errors = _fetch_positions_from_client(client, wallet)
    if client_positions:
        positions = client_positions
    errors.extend(client_errors)

    if not positions:
        http_positions, http_errors = _fetch_positions_via_http(wallet)
        if http_positions:
            positions = http_positions
        errors.extend(http_errors)

    if args.limit and args.limit > 0:
        positions = positions[: args.limit]

    if args.json:
        output = [
            {
                "token_id": rec.token_id,
                "direction": rec.direction,
                "quantity": rec.quantity,
                "avg_price": rec.avg_price,
                "mark_price": rec.mark_price,
                "cost": rec.cost,
                "mark_value": rec.mark_value,
                "pnl": rec.pnl,
                "market_question": rec.market_question,
                "market_slug": rec.market_slug,
                "market_url": rec.market_url,
                "last_update": rec.last_update,
            }
            for rec in positions
        ]
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        positions.sort(
            key=lambda r: (
                (r.market_question or r.market_slug or ""),
                r.direction,
                r.token_id,
            )
        )
        print("")
        print(_build_table(positions))
        if positions:
            _print_summary(positions)

    if args.verbose and errors:
        print("\n[DEBUG] 尝试过的接口：")
        for err in errors:
            print(f"  - {err}")

    if not positions:
        print("[WARN] 未能获取任何持仓记录，请检查网络或 API 凭据。")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())

