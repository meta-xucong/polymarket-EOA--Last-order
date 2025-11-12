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
from decimal import Decimal
from functools import lru_cache
from typing import Any, Dict, Optional, Set, Tuple

from Volatility_fliter_EOA import (
    HighlightedOutcome,
    collect_filter_results,
)
from Volatility_buy_EOA import execute_auto_buy
from trading.execution import ExecutionResult

from web3 import Web3
from web3.middleware import geth_poa_middleware


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


_MIN_USDCE_BALANCE = 5.0

_DEFAULT_POLYGON_RPC = "https://polygon-rpc.com"

_ADDR_ENV_CANDIDATES = (
    "POLY_EOA_ADDRESS",
    "POLY_ADDRESS",
    "POLY_WALLET",
)

_KEY_ENV_CANDIDATES = (
    "POLY_EOA_KEY",
    "POLY_KEY",
    "POLY_PRIVATE_KEY",
    "PRIVATE_KEY",
)

_RPC_ENV_CANDIDATES = (
    "POLY_RPC_URL",
    "POLYGON_RPC",
    "POLY_RPC",
    "RPC_URL",
)

_USDCE_ENV_CANDIDATES = (
    "POLY_USDC_ADDRESS",
    "USDCe_ADDRESS",
    "USDCE_ADDRESS",
    "USDC_ADDRESS",
)

_DEFAULT_USDCE_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

_ERC20_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def _normalize_evm_address(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if not text.startswith(("0x", "0X")):
        if len(text) == 40:
            text = "0x" + text
        else:
            return None
    if len(text) != 42:
        return None
    try:
        return Web3.to_checksum_address(text)
    except Exception:
        return None


def _derive_address_from_key(raw_key: Optional[str]) -> Optional[str]:
    if not isinstance(raw_key, str):
        return None
    text = raw_key.strip()
    if not text:
        return None
    if text.startswith(("0x", "0X")):
        text = text[2:]
    if len(text) != 64:
        return None
    try:
        from eth_account import Account  # type: ignore
    except Exception:
        return None
    try:
        account = Account.from_key(bytes.fromhex(text))
    except Exception:
        return None
    return _normalize_evm_address(getattr(account, "address", None))


def _normalize_rpc_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _resolve_usdce_address() -> str:
    for env in _USDCE_ENV_CANDIDATES:
        candidate = _normalize_evm_address(os.getenv(env))
        if candidate:
            return candidate
    fallback = _normalize_evm_address(_DEFAULT_USDCE_ADDRESS)
    if fallback:
        return fallback
    return _DEFAULT_USDCE_ADDRESS


def _infer_wallet_address(client) -> Optional[str]:
    attr_candidates = (
        "wallet_address",
        "wallet",
        "address",
        "account",
        "owner",
        "funder",
        "trader_address",
        "eoa_address",
    )
    for attr in attr_candidates:
        if not hasattr(client, attr):
            continue
        try:
            value = getattr(client, attr)
        except Exception:
            continue
        normalized = _normalize_evm_address(value)
        if normalized:
            return normalized

    getter_candidates = (
        "get_wallet_address",
        "get_address",
        "get_account_address",
        "get_default_address",
    )
    for name in getter_candidates:
        fn = getattr(client, name, None)
        if not callable(fn):
            continue
        try:
            value = fn()
        except Exception:
            continue
        normalized = _normalize_evm_address(value)
        if normalized:
            return normalized

    for env in _ADDR_ENV_CANDIDATES:
        normalized = _normalize_evm_address(os.getenv(env))
        if normalized:
            return normalized

    for env in _KEY_ENV_CANDIDATES:
        normalized = _derive_address_from_key(os.getenv(env))
        if normalized:
            return normalized

    return None


@lru_cache(maxsize=8)
def _get_web3(rpc_url: str) -> Web3:
    provider = Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20})
    w3 = Web3(provider)
    try:
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
    except ValueError:
        pass
    return w3


def _infer_rpc_url(client) -> str:
    attr_candidates = (
        "rpc_url",
        "rpc",
        "polygon_rpc",
        "rpc_endpoint",
        "provider_url",
    )
    for attr in attr_candidates:
        if not hasattr(client, attr):
            continue
        try:
            value = getattr(client, attr)
        except Exception:
            continue
        normalized = _normalize_rpc_url(value)
        if normalized:
            return normalized

    getter_candidates = (
        "get_rpc_url",
        "get_polygon_rpc",
        "get_provider_url",
    )
    for name in getter_candidates:
        fn = getattr(client, name, None)
        if not callable(fn):
            continue
        try:
            value = fn()
        except Exception:
            continue
        normalized = _normalize_rpc_url(value)
        if normalized:
            return normalized

    for env in _RPC_ENV_CANDIDATES:
        normalized = _normalize_rpc_url(os.getenv(env))
        if normalized:
            return normalized

    return _DEFAULT_POLYGON_RPC


def _fetch_available_quote_balance(client) -> Optional[float]:
    """通过链上查询当前钱包地址的 USDC.e 余额。"""

    address = _infer_wallet_address(client)
    if not address:
        raise RuntimeError(
            "无法确定 EOA 地址，请设置 POLY_EOA_ADDRESS / POLY_ADDRESS 或提供私钥以导出。"
        )

    rpc_url = _infer_rpc_url(client)
    w3 = _get_web3(rpc_url)
    if not w3.is_connected():
        raise RuntimeError(f"无法连接到 Polygon RPC: {rpc_url}")

    checksum_owner = w3.to_checksum_address(address)
    token_address = _resolve_usdce_address()
    checksum_token = w3.to_checksum_address(token_address)
    contract = w3.eth.contract(address=checksum_token, abi=_ERC20_BALANCE_ABI)

    try:
        raw_balance = contract.functions.balanceOf(checksum_owner).call()
    except Exception as exc:
        raise RuntimeError(f"balanceOf 调用失败：{exc}") from exc

    decimals = 6
    try:
        queried = contract.functions.decimals().call()
    except Exception:
        queried = None
    if queried is not None:
        try:
            decimals = max(0, int(queried))
        except (TypeError, ValueError):
            decimals = 6

    try:
        balance_dec = Decimal(int(raw_balance)) / (Decimal(10) ** decimals)
    except Exception as exc:
        raise RuntimeError(f"无法换算余额：{exc}") from exc

    return float(balance_dec)


def _ensure_minimum_usdce_balance(client) -> bool:
    available = None
    try:
        available = _fetch_available_quote_balance(client)
    except Exception as exc:  # pragma: no cover - 仅运行时提示
        print(f"[ERR] 获取 USDC.e 余额失败：{exc}")
        print("余额获取失败，请检查原因")
        return False

    if available is None:
        print("余额获取失败，请检查原因")
        return False

    print(f"[INFO] 当前 USDC.e 可用余额：{available:.4f}")
    if available + 1e-9 < _MIN_USDCE_BALANCE:
        print("余额太少，本轮跳过")
        return False
    return True


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


def _normalize_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _load_existing_positions() -> Tuple[Set[str], Set[str]]:
    try:
        from view_positions_EOA import _fetch_positions_eoa, _infer_wallet_address
    except Exception as exc:
        print(f"[WARN] 无法导入持仓查询模块，跳过持仓检查：{exc}")
        return set(), set()

    wallet = _infer_wallet_address()
    if not wallet:
        print("[WARN] 未能确定钱包地址，跳过持仓去重。")
        return set(), set()

    try:
        positions = _fetch_positions_eoa(wallet)
    except Exception as exc:
        print(f"[WARN] 获取当前持仓失败：{exc}，将不执行去重。")
        return set(), set()

    market_slugs: Set[str] = set()
    token_ids: Set[str] = set()

    for pos in positions:
        if not isinstance(pos, dict):
            continue

        for key in (
            "marketSlug",
            "market_slug",
            "slug",
            "eventSlug",
            "event_slug",
            "collectionSlug",
            "collection_slug",
        ):
            slug = _normalize_string(pos.get(key))
            if slug:
                market_slugs.add(slug)
                break

        for key in (
            "token_id",
            "tokenId",
            "asset",
            "token",
            "outcomeToken",
            "outcome_token",
        ):
            token = _normalize_string(pos.get(key))
            if token:
                token_ids.add(token)
                break

    print(
        f"[INFO] 已加载当前持仓：共 {len(market_slugs)} 个市场，{len(token_ids)} 个 token。"
    )
    return market_slugs, token_ids


def _run_claim_workflow() -> Optional[int]:
    try:
        from claim_all_markets_EOA import main as claim_main
    except Exception as exc:
        print(f"[WARN] 无法导入 claim_all_markets_EOA：{exc}。请稍后手动执行。")
        return None

    print("[STEP] 正在执行 Claim 检查…")
    try:
        return claim_main([])
    except SystemExit as exc:  # 防止内部调用 sys.exit
        code = exc.code if isinstance(exc.code, int) else 1
        return code
    except Exception as exc:
        print(f"[ERR] Claim 流程异常：{exc}")
        return -1


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

    print("[STEP] 初始化 CLOB 客户端…")
    client = _get_client()
    if not _ensure_minimum_usdce_balance(client):
        return

    print("[STEP] 正在运行市场筛选器…")
    result = collect_filter_results()
    highlights = list(result.highlights)
    print(
        f"[INFO] 筛选完成：候选 {len(result.chosen)} / 总计 {result.total_markets}，"
        f"满足严格条件 {len(highlights)} 项。"
    )

    existing_market_slugs, existing_token_ids = _load_existing_positions()

    if not highlights:
        print("[INFO] 当前无满足条件的市场。")
        claim_code = _run_claim_workflow()
        if claim_code is not None:
            status = "成功" if claim_code == 0 else f"退出码 {claim_code}"
            print(f"[INFO] Claim 流程已执行：{status}。")
        return

    print("[INFO] 命中列表：")
    for idx, ho in enumerate(highlights, start=1):
        print(_format_highlight(ho, idx))

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
        slug = ho.market.slug if getattr(ho.market, "slug", None) else None
        if not token_id or ask_price is None:
            print(f"[SKIP] #{idx} 缺少 token_id 或卖价，跳过。")
            continue

        token_id_str = str(token_id)
        if slug and slug in existing_market_slugs:
            print(f"[SKIP] #{idx} 市场 {slug} 已存在持仓，跳过买入。")
            continue
        if token_id_str and token_id_str in existing_token_ids:
            print(f"[SKIP] #{idx} token_id={token_id_str} 已存在持仓，跳过买入。")
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

        if slug:
            existing_market_slugs.add(slug)
        if token_id_str:
            existing_token_ids.add(token_id_str)

    claim_code = _run_claim_workflow()
    if claim_code is not None:
        status = "成功" if claim_code == 0 else f"退出码 {claim_code}"
        print(f"[INFO] Claim 流程已执行：{status}。")
    print("[DONE] 买单流程结束。")


if __name__ == "__main__":
    main()
