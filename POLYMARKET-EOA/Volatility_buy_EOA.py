# Volatility_buy_EOA.py
# -*- coding: utf-8 -*-
"""
EOA 版批量买单执行器：复用 Safe 版本的拆单、退让、余额兜底等完整逻辑，
通过 CLOB EOA 客户端下单。
"""

from collections.abc import Iterable as IterableABC, Mapping as MappingABC
from decimal import Decimal, ROUND_UP
from functools import lru_cache
from typing import Any, Optional, Tuple

from trading.execution import (
    ClobPolymarketAPI,
    ExecutionConfig,
    ExecutionEngine,
    ExecutionResult,
    load_default_config,
)

_NOTIONAL_BUFFER_RATIO = 0.01


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _extract_available_quote(payload: Any) -> Optional[float]:
    numeric = _coerce_float(payload)
    if numeric is not None:
        return numeric

    if isinstance(payload, MappingABC):
        priority_keys = (
            "available_quote",
            "availableQuote",
            "available",
            "available_amount",
            "availableAmount",
            "freeCollateral",
            "free_collateral",
            "quote_available",
            "quoteAvailable",
            "cash_balance",
            "cashBalance",
            "cash",
            "balance",
            "quote_balance",
            "quoteBalance",
        )
        for key in priority_keys:
            if key in payload:
                extracted = _extract_available_quote(payload[key])
                if extracted is not None:
                    return extracted

        asset_keys = (
            "USDC",
            "usdc",
            "USD",
            "usd",
            "quote",
            "QUOTE",
        )
        for key in asset_keys:
            if key in payload:
                extracted = _extract_available_quote(payload[key])
                if extracted is not None:
                    return extracted

        container_keys = (
            "balances",
            "wallet",
            "wallets",
            "assets",
            "data",
            "result",
            "availableBalances",
            "balancesMap",
        )
        for key in container_keys:
            if key in payload:
                extracted = _extract_available_quote(payload[key])
                if extracted is not None:
                    return extracted

        for value in payload.values():
            extracted = _extract_available_quote(value)
            if extracted is not None:
                return extracted
        return None

    if isinstance(payload, IterableABC) and not isinstance(payload, (str, bytes, bytearray)):
        for item in payload:
            extracted = _extract_available_quote(item)
            if extracted is not None:
                return extracted
        return None

    return None


def _fetch_available_quote_balance(client) -> Optional[float]:
    """Best-effort retrieval of the quote currency balance from the client."""

    method_candidates = [
        ("get_balances", {}),
        ("get_balance", {}),
        ("get_wallet_balances", {}),
        ("get_wallet_balance", {}),
        ("get_user_balances", {}),
        ("get_user_balance", {}),
        ("get_user_account", {}),
        ("get_account", {}),
        ("get_accounts", {}),
        ("get_user", {}),
        ("get_profile", {}),
        ("get_trading_balance", {}),
        ("get_trading_balances", {}),
    ]

    for name, kwargs in method_candidates:
        fn = getattr(client, name, None)
        if not callable(fn):
            continue
        try:
            resp = fn(**kwargs)
        except TypeError:
            continue
        except Exception:
            continue

        payload = resp
        if isinstance(resp, tuple) and len(resp) == 2:
            payload = resp[1]
        if isinstance(payload, MappingABC) and "data" in payload and "status" in payload:
            payload = payload.get("data")

        amount = _extract_available_quote(payload)
        if amount is not None:
            return float(amount)

    return None


def _q2_up(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_UP)


def _q4_up(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.0001"), rounding=ROUND_UP)


def _ceil_int(x: Decimal) -> Decimal:
    return x.quantize(Decimal("1"), rounding=ROUND_UP)


def _quantize_up(value: Decimal, quantum: Decimal) -> Decimal:
    if quantum <= 0:
        return value
    quotient = (value / quantum).to_integral_value(rounding=ROUND_UP)
    result = quotient * quantum
    return result.quantize(quantum, rounding=ROUND_UP)


def _min_legal_pair(
    price: float,
    size: float,
    *,
    minimum_size: float = 0.0,
    tick_size: float = 0.0,
) -> Tuple[float, float, float]:
    """生成最小合法组合（全部向上取整，满足 maker amount ≤ 2dp 约束）"""

    tick = Decimal("0.01")
    if tick_size and tick_size > 0:
        tick = Decimal(str(tick_size))
    p = _quantize_up(Decimal(str(price)), tick)

    s_hint = Decimal(str(size))

    if p > 0:
        s_need_int = _ceil_int(Decimal("1.00") / p)
    else:
        s_need_int = Decimal("1")

    if (s_hint % 1) != 0:
        eff_size = _q4_up(s_hint)
    else:
        eff_size = _ceil_int(s_hint)

    if eff_size < s_need_int:
        eff_size = s_need_int

    if minimum_size and minimum_size > 0:
        min_size_dec = _q4_up(Decimal(str(minimum_size)))
        if eff_size < min_size_dec:
            eff_size = min_size_dec

    maker = _q2_up(p * eff_size)        # 两位小数（仅日志用）

    return float(p), float(eff_size), float(maker)


def _extract_best_ask(payload: Any) -> Optional[float]:
    numeric = _coerce_float(payload)
    if numeric is not None:
        return numeric

    if isinstance(payload, MappingABC):
        primary_keys = (
            "best_ask",
            "bestAsk",
            "ask",
            "offer",
            "best_offer",
            "bestOffer",
            "lowest_ask",
            "lowestAsk",
            "sell",
        )
        for key in primary_keys:
            if key in payload:
                extracted = _extract_best_ask(payload[key])
                if extracted is not None:
                    return extracted

        ladder_keys = (
            "asks",
            "ask_levels",
            "sell_orders",
            "sellOrders",
            "offers",
        )
        for key in ladder_keys:
            if key in payload:
                ladder = payload[key]
                if isinstance(ladder, IterableABC) and not isinstance(
                    ladder, (str, bytes, bytearray)
                ):
                    for entry in ladder:
                        if isinstance(entry, MappingABC) and "price" in entry:
                            candidate = _coerce_float(entry["price"])
                            if candidate is not None:
                                return candidate
                        extracted = _extract_best_ask(entry)
                        if extracted is not None:
                            return extracted

        for value in payload.values():
            extracted = _extract_best_ask(value)
            if extracted is not None:
                return extracted
        return None

    if isinstance(payload, IterableABC) and not isinstance(payload, (str, bytes, bytearray)):
        for item in payload:
            extracted = _extract_best_ask(item)
            if extracted is not None:
                return extracted
        return None

    return None


def _fetch_best_ask_price(client, token_id: str) -> Optional[float]:
    """Best-effort retrieval of the current best ask for the given market."""

    method_candidates = (
        ("get_market_orderbook", {"market": token_id}),
        ("get_market_orderbook", {"token_id": token_id}),
        ("get_market_orderbook", {"market_id": token_id}),
        ("get_order_book", {"market": token_id}),
        ("get_order_book", {"token_id": token_id}),
        ("get_orderbook", {"market": token_id}),
        ("get_orderbook", {"token_id": token_id}),
        ("get_market", {"market": token_id}),
        ("get_market", {"token_id": token_id}),
        ("get_market_data", {"market": token_id}),
        ("get_market_data", {"token_id": token_id}),
        ("get_ticker", {"market": token_id}),
        ("get_ticker", {"token_id": token_id}),
    )

    for name, kwargs in method_candidates:
        fn = getattr(client, name, None)
        if not callable(fn):
            continue
        try:
            resp = fn(**kwargs)
        except TypeError:
            continue
        except Exception:
            continue

        payload = resp
        if isinstance(resp, tuple) and len(resp) == 2:
            payload = resp[1]
        if isinstance(payload, MappingABC) and "data" in payload and "status" in payload:
            payload = payload.get("data")

        best_ask = _extract_best_ask(payload)
        if best_ask is not None:
            return float(best_ask)

    return None


def _enforce_market_minimums(
    price: float,
    size: float,
    *,
    best_ask: Optional[float] = None,
    tick_size: float = 0.0,
    min_quote: float = 1.0,
    buffer_ratio: float = _NOTIONAL_BUFFER_RATIO,
) -> Tuple[Decimal, Decimal]:
    """Ensure the order satisfies market notional constraints with a safety buffer."""

    try:
        price_dec = Decimal(str(price))
        size_dec = Decimal(str(size))
    except Exception:
        return Decimal(str(size or 0.0)), Decimal(str(max(min_quote, 0.0)))

    if price_dec <= 0:
        min_quote_dec = Decimal(str(min_quote)) if min_quote and min_quote > 0 else Decimal("0")
        return _q4_up(size_dec), _q2_up(min_quote_dec)

    min_quote_dec = Decimal(str(min_quote)) if min_quote and min_quote > 0 else Decimal("1.0")
    buffer_ratio = buffer_ratio if buffer_ratio and buffer_ratio > 0 else 0.0
    buffer_dec = Decimal(str(buffer_ratio))
    target_notional = min_quote_dec * (Decimal("1") + buffer_dec)

    price_buffer = Decimal("0.0001")
    if tick_size and tick_size > 0:
        try:
            tick_dec = Decimal(str(tick_size))
            if tick_dec > 0:
                price_buffer = max(price_buffer, tick_dec)
        except Exception:
            pass

    price_reference = price_dec
    best_ask_dec: Optional[Decimal] = None
    if best_ask is not None:
        try:
            best_ask_dec = Decimal(str(best_ask))
        except Exception:
            best_ask_dec = None
    if best_ask_dec is not None and best_ask_dec > 0:
        price_reference = min(price_reference, best_ask_dec)

    price_floor = price_reference
    if price_reference > price_buffer:
        price_floor = price_reference - price_buffer
    elif price_reference > Decimal("0"):
        price_floor = price_reference * Decimal("0.999")

    if price_floor <= Decimal("0"):
        price_floor = price_reference if price_reference > Decimal("0") else Decimal("0")

    size_dec = _q4_up(size_dec)

    required_size = Decimal("0")
    if price_dec > Decimal("0") and min_quote_dec > Decimal("0"):
        required_size = _q4_up(min_quote_dec / price_dec)

    if price_floor > Decimal("0") and target_notional > Decimal("0"):
        buffered_required = _q4_up(target_notional / price_floor)
        if buffered_required > required_size:
            required_size = buffered_required

    if required_size > size_dec:
        size_dec = required_size

    if required_size <= Decimal("0"):
        required_size = size_dec

    min_quote_override = _q2_up(price_dec * required_size)
    min_quote_floor_dec = _q2_up(min_quote_dec) if min_quote_dec > Decimal("0") else Decimal("0")
    if min_quote_override < min_quote_floor_dec:
        min_quote_override = min_quote_floor_dec

    return size_dec, min_quote_override


@lru_cache()
def _load_config(config_path: str = "") -> ExecutionConfig:
    config = load_default_config(config_path or None)
    return config


def _build_engine(client) -> ExecutionEngine:
    config = _load_config()
    return ExecutionEngine(ClobPolymarketAPI(client), config)


def execute_auto_buy(
    client,
    token_id: str,
    price: float,
    size: float,
    *,
    min_order_size: float = 0.0,
    tick_size: float = 0.0,
) -> ExecutionResult:
    eff_price, eff_size, maker = _min_legal_pair(
        price,
        size,
        minimum_size=min_order_size,
        tick_size=tick_size,
    )
    engine = _build_engine(client)

    original_min_market = getattr(engine.config, "min_market_order_size", 0.0)
    original_min_quote = getattr(engine.config, "min_quote_amount", 0.0)
    original_slice_min = engine.config.order_slice_min
    original_slice_max = engine.config.order_slice_max

    min_quote_floor = original_min_quote if original_min_quote and original_min_quote > 0 else 1.0
    best_ask = _fetch_best_ask_price(client, token_id)
    size_dec, min_quote_override = _enforce_market_minimums(
        eff_price,
        eff_size,
        best_ask=best_ask,
        tick_size=tick_size,
        min_quote=min_quote_floor,
    )
    eff_size = float(size_dec)
    maker = float(_q2_up(Decimal(str(eff_price)) * size_dec))
    engine.config.min_quote_amount = float(min_quote_override)

    extra_flags = []
    if min_order_size and min_order_size > 0:
        extra_flags.append(f"min_order_size={min_order_size}")
    if tick_size and tick_size > 0:
        extra_flags.append(f"tick_size={tick_size}")
    if best_ask is not None and best_ask > 0:
        extra_flags.append(f"best_ask={best_ask}")
    min_quote_effective = float(min_quote_override)
    if original_min_quote is None or min_quote_effective > float(original_min_quote) + 1e-9:
        extra_flags.append(f"min_quote_eff={min_quote_effective}")
    tail = f" | {' '.join(extra_flags)}" if extra_flags else ""
    print(
        "[Volatility_buy_EOA] 规范化 -> "
        f"base_price={price} | hint_size={size} | eff_price={eff_price} | eff_size={eff_size} | maker={maker}" + tail
    )

    try:
        effective_min_order: Optional[float]
        try:
            effective_min_order = float(min_order_size)
        except (TypeError, ValueError):
            effective_min_order = None
        if effective_min_order and effective_min_order > 0:
            engine.config.min_market_order_size = effective_min_order
            if engine.config.order_slice_min < effective_min_order:
                engine.config.order_slice_min = effective_min_order
            if engine.config.order_slice_max < engine.config.order_slice_min:
                engine.config.order_slice_max = engine.config.order_slice_min

        available_quote = _fetch_available_quote_balance(client)
        if available_quote is not None:
            try:
                slice_plan = list(
                    engine._slice_quantities(
                        float(eff_size), side="buy", price=float(eff_price)
                    )
                )
            except Exception:
                slice_plan = [float(eff_size)]
            slice_plan = [s for s in slice_plan if s and s > 0]
            first_slice = slice_plan[0] if slice_plan else float(eff_size)
            min_quote_needed = float(eff_price) * first_slice
            total_quote_needed = float(eff_price) * float(eff_size)
            eps = 1e-9

            if available_quote + eps < min_quote_needed:
                print(
                    f"[Volatility_buy_EOA] 余额 {available_quote:.4f} USDC 低于最小下单需求 {min_quote_needed:.4f}，跳过本次买入。"
                )
                return ExecutionResult(
                    side="buy",
                    requested=float(eff_size),
                    filled=0.0,
                    last_price=float(eff_price),
                    attempts=0,
                    status="SKIPPED",
                    message=(
                        f"INSUFFICIENT_FUNDS_MIN_SLICE(required={min_quote_needed:.4f}, available={available_quote:.4f})"
                    ),
                    avg_price=None,
                    limit_price=float(eff_price),
                )

            if available_quote + eps < total_quote_needed:
                print(
                    f"[Volatility_buy_EOA] 余额 {available_quote:.4f} USDC 无法覆盖本次买入所需 {total_quote_needed:.4f}，跳过本次买入。"
                )
                return ExecutionResult(
                    side="buy",
                    requested=float(eff_size),
                    filled=0.0,
                    last_price=float(eff_price),
                    attempts=0,
                    status="SKIPPED",
                    message=(
                        f"INSUFFICIENT_FUNDS_TOTAL(required={total_quote_needed:.4f}, available={available_quote:.4f})"
                    ),
                    avg_price=None,
                    limit_price=float(eff_price),
                )

        result = engine.execute_buy(
            token_id=str(token_id),
            price=float(eff_price),
            quantity=float(eff_size),
        )
    finally:
        engine.config.min_market_order_size = original_min_market
        engine.config.min_quote_amount = original_min_quote
        engine.config.order_slice_min = original_slice_min
        engine.config.order_slice_max = original_slice_max

    print(
        "[Volatility_buy_EOA] 执行结果 -> "
        f"status={result.status} filled={result.filled} requested={result.requested} "
        f"price={result.last_price} limit={result.limit_price}"
    )
    return result


__all__ = ["execute_auto_buy"]
