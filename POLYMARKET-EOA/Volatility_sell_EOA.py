# Volatility_sell_EOA.py
# -*- coding: utf-8 -*-
"""
EOA 版卖单执行器，对应 execute_auto_sell 接口。
"""

from decimal import Decimal, ROUND_DOWN
from typing import Tuple

from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import SELL


def _q2_down(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _q4_down(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def _min_legal_pair(price: float, size: float) -> Tuple[float, float, float]:
    p = _q2_down(Decimal(str(price)))
    s_hint = Decimal(str(size))

    if (s_hint % 1) != 0:
        eff_size = _q4_down(s_hint)
    else:
        eff_size = s_hint
    if eff_size <= 0:
        raise ValueError("卖出份数必须大于 0")

    maker = _q2_down(p * eff_size)

    return float(p), float(eff_size), float(maker)


def execute_auto_sell(client, token_id: str, price: float, size: float):
    eff_price, eff_size, maker = _min_legal_pair(price, size)
    print(
        f"[Volatility_sell_EOA] 规范化 -> base_price={price} | hint_size={size} | "
        f"eff_price={eff_price} | eff_size={eff_size} | maker={maker}"
    )
    order = OrderArgs(token_id=str(token_id), side=SELL, price=float(eff_price), size=float(eff_size))
    print(f"[Volatility_sell_EOA] create_order SELL token_id={token_id} price={eff_price} size={eff_size}")
    signed = client.create_order(order)
    print("[Volatility_sell_EOA] post_order type=FAK")
    return client.post_order(signed, OrderType.FAK)


__all__ = ["execute_auto_sell"]
