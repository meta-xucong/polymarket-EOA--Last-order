# Volatility_buy_EOA.py
# -*- coding: utf-8 -*-
"""
EOA 版买单执行器。逻辑与 Safe 版保持一致，用于与 EOA 客户端搭配。
"""

from decimal import Decimal, ROUND_UP
from typing import Tuple

from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY


def _q2_up(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_UP)


def _q4_up(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.0001"), rounding=ROUND_UP)


def _ceil_int(x: Decimal) -> Decimal:
    return x.quantize(Decimal("1"), rounding=ROUND_UP)


def _min_legal_pair(price: float, size: float) -> Tuple[float, float, float]:
    p = _q2_up(Decimal(str(price)))
    s_hint = Decimal(str(size))

    s_need_int = _ceil_int(Decimal("1.00") / p)
    if (s_hint % 1) != 0:
        eff_size = _q4_up(s_hint)
    else:
        eff_size = _ceil_int(s_hint)
    if eff_size < s_need_int:
        eff_size = s_need_int

    maker = _q2_up(p * eff_size)

    return float(p), float(eff_size), float(maker)


def execute_auto_buy(client, token_id: str, price: float, size: float):
    eff_price, eff_size, maker = _min_legal_pair(price, size)
    print(
        f"[Volatility_buy_EOA] 规范化 -> base_price={price} | hint_size={size} | "
        f"eff_price={eff_price} | eff_size={eff_size} | maker={maker}"
    )
    order = OrderArgs(token_id=str(token_id), side=BUY, price=float(eff_price), size=float(eff_size))
    print(f"[Volatility_buy_EOA] create_order BUY token_id={token_id} price={eff_price} size={eff_size}")
    signed = client.create_order(order)
    print("[Volatility_buy_EOA] post_order type=FAK")
    return client.post_order(signed, OrderType.FAK)


__all__ = ["execute_auto_buy"]
