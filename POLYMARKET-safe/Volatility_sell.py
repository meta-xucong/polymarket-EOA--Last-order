
# Volatility_sell.py
# -*- coding: utf-8 -*-
"""
批量卖单执行器，使用 :mod:`trading.execution` 提供的调度能力。

- 支持拆单、部分成交与重试退让逻辑；
- 数量继续在本地按两位小数向下取整，避免 dust；
- 返回 :class:`trading.execution.ExecutionResult`，方便上层统一处理。
"""

import math
from functools import lru_cache

from trading.execution import (
    ClobPolymarketAPI,
    ExecutionConfig,
    ExecutionEngine,
    ExecutionResult,
    load_default_config,
)

__all__ = ["execute_auto_sell"]


def _floor_2dp(x: float) -> float:
    return math.floor(float(x) * 100.0) / 100.0


@lru_cache()
def _load_config(config_path: str = "") -> ExecutionConfig:
    return load_default_config(config_path or None)


def _build_engine(client) -> ExecutionEngine:
    config = _load_config()
    return ExecutionEngine(ClobPolymarketAPI(client), config)


def execute_auto_sell(
    client,
    token_id: str,
    price: float,
    size: float,
) -> ExecutionResult:
    size_real = _floor_2dp(size)
    if size_real < 0.01:
        print("[Volatility_sell] size < 0.01 after 2dp floor, skip.")
        return ExecutionResult(
            side="sell",
            requested=float(size),
            filled=0.0,
            last_price=float(price),
            attempts=0,
            status="SKIPPED",
            message="SIZE_TOO_SMALL",
            avg_price=None,
            limit_price=float(price),
        )

    engine = _build_engine(client)
    result = engine.execute_sell(token_id=str(token_id), price=float(price), quantity=float(size_real))
    print(
        "[Volatility_sell] 执行结果 -> "
        f"status={result.status} filled={result.filled} requested={result.requested} "
        f"price={result.last_price} limit={result.limit_price}"
    )
    return result
