# Volatility_arbitrage_strategy_EOA.py
# -*- coding: utf-8 -*-
"""
Volatility_arbitrage_strategy_EOA
=================================

原 Safe 版策略模块在当前仓库中并不存在，之前的 EOA 文件
(`Volatility_arbitrage_strategy_EOA.py`) 仅通过 ``import
Volatility_arbitrage_strategy`` 的方式复用旧代码。这样会在执行
``Volatility_arbitrage_run_EOA.py`` 时触发 ``ModuleNotFoundError``，
导致整套 EOA 策略无法启动。

本文件实现了必要的策略状态机，提供 ``StrategyConfig``、
``Action``、``ActionType`` 与 ``VolArbStrategy`` 四个对象，满足
运行脚本的依赖，并保持与旧版逻辑一致的交互接口。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Deque, Dict, Optional
from collections import deque
import time


class ActionType(Enum):
    """策略需要执行的动作类型。"""

    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Action:
    """策略触发的动作指令。"""

    action: ActionType
    reason: str
    ref_price: Optional[float] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class StrategyConfig:
    """策略配置参数。"""

    token_id: str
    buy_price_threshold: Optional[float]
    drop_window_minutes: float
    drop_pct: float
    profit_pct: float

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


@dataclass
class _PricePoint:
    ts: float
    price: float


class VolArbStrategy:
    """简单的波动套利状态机。"""

    def __init__(self, config: StrategyConfig):
        self.config = config
        self._state: str = "FLAT"
        self._awaiting: Optional[ActionType] = None
        self._entry_price: Optional[float] = None
        self._position_size: Optional[float] = None
        self._sell_trigger: Optional[float] = None
        self._stopped: bool = False
        self._stop_reason: Optional[str] = None
        self._last_signal: Optional[str] = None
        self._last_reject: Optional[str] = None
        self._last_fill: Optional[str] = None
        self._prices: Deque[_PricePoint] = deque()
        self._window_seconds = max(float(config.drop_window_minutes or 0) * 60.0, 0.0)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _record_price(self, ts: float, price: Optional[float]) -> None:
        if price is None or price <= 0:
            return
        if ts <= 0:
            ts = time.time()
        self._prices.append(_PricePoint(ts=ts, price=float(price)))
        if self._window_seconds <= 0:
            return
        cutoff = ts - self._window_seconds
        while self._prices and self._prices[0].ts < cutoff:
            self._prices.popleft()

    def _max_price(self) -> Optional[float]:
        if not self._prices:
            return None
        return max(p.price for p in self._prices)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Optional[float]]:
        return {
            "state": self._state,
            "awaiting": self._awaiting,
            "entry_price": self._entry_price,
            "position_size": self._position_size,
            "sell_trigger": self._sell_trigger,
            "stopped": self._stopped,
            "stop_reason": self._stop_reason,
            "last_signal": self._last_signal,
            "last_reject": self._last_reject,
            "last_fill": self._last_fill,
            "config": self.config.to_dict(),
        }

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def stop(self, reason: Optional[str] = None) -> None:
        self._stopped = True
        self._state = "STOPPED"
        self._awaiting = None
        self._stop_reason = reason or self._stop_reason

    def on_tick(self, best_ask: Optional[float], best_bid: Optional[float], ts: float) -> Optional[Action]:
        if self._stopped:
            return None

        ref_buy_price = best_ask if best_ask and best_ask > 0 else best_bid
        ref_sell_price = best_bid if best_bid and best_bid > 0 else best_ask
        price_for_history = ref_buy_price if ref_buy_price and ref_buy_price > 0 else ref_sell_price

        self._record_price(ts, price_for_history)

        if self._awaiting is not None:
            return None

        if self._state in {"FLAT", "WAIT_BUY"}:
            trigger_price = None
            trigger_reason = None
            if (
                ref_buy_price is not None
                and self.config.buy_price_threshold is not None
                and ref_buy_price <= float(self.config.buy_price_threshold)
            ):
                trigger_price = ref_buy_price
                trigger_reason = (
                    f"price {ref_buy_price:.4f} <= threshold {float(self.config.buy_price_threshold):.4f}"
                )
            elif (
                ref_buy_price is not None
                and self.config.drop_pct
                and self.config.drop_pct > 0
            ):
                max_price = self._max_price()
                if max_price and max_price > 0:
                    target_price = max_price * (1.0 - float(self.config.drop_pct))
                    if ref_buy_price <= target_price:
                        trigger_price = ref_buy_price
                        pct = self.config.drop_pct * 100.0
                        trigger_reason = (
                            f"price drop {pct:.2f}% from {max_price:.4f} to {ref_buy_price:.4f}"
                        )

            if trigger_price is not None and trigger_reason is not None:
                self._awaiting = ActionType.BUY
                self._state = "WAIT_BUY"
                self._last_signal = trigger_reason
                return Action(action=ActionType.BUY, reason=trigger_reason, ref_price=trigger_price)

        if self._state in {"LONG", "WAIT_SELL"}:
            target = self._sell_trigger
            if target is None and self._entry_price is not None and self.config.profit_pct:
                target = self._entry_price * (1.0 + float(self.config.profit_pct))
                self._sell_trigger = target

            if target and ref_sell_price and ref_sell_price >= target:
                reason = f"price {ref_sell_price:.4f} >= target {target:.4f}"
                self._awaiting = ActionType.SELL
                self._state = "WAIT_SELL"
                self._last_signal = reason
                return Action(action=ActionType.SELL, reason=reason, ref_price=ref_sell_price)

        return None

    def on_buy_filled(self, avg_price: Optional[float], size: Optional[float]) -> None:
        if avg_price is not None:
            self._entry_price = float(avg_price)
        self._position_size = float(size) if size is not None else self._position_size
        if self._entry_price is not None and self.config.profit_pct:
            self._sell_trigger = self._entry_price * (1.0 + float(self.config.profit_pct))
        else:
            self._sell_trigger = None
        self._awaiting = None
        self._state = "LONG"
        self._last_fill = f"BUY@{self._entry_price}" if self._entry_price is not None else "BUY"

    def on_sell_filled(self, avg_price: Optional[float]) -> None:
        if avg_price is not None:
            self._last_fill = f"SELL@{float(avg_price):.4f}"
        else:
            self._last_fill = "SELL"
        self._awaiting = None
        self._state = "FLAT"
        self._entry_price = None
        self._position_size = None
        self._sell_trigger = None

    def on_reject(self, reason: Optional[str]) -> None:
        self._last_reject = reason
        if self._awaiting == ActionType.BUY:
            self._state = "FLAT"
        elif self._awaiting == ActionType.SELL:
            self._state = "LONG"
        self._awaiting = None


__all__ = ["Action", "ActionType", "StrategyConfig", "VolArbStrategy"]
