
# Volatility_arbitrage_strategy.py
# 极简策略（扩展版）：
#   - 在窗口内跟踪价格高点，若当前价相对高点下跌超过 drop_pct，触发 BUY；
#   - 可选 buy_price_threshold 作为额外保底条件；
#   - 持仓后以 profit_pct（默认 5%）的涨幅目标触发 SELL；
#   - 仅产出信号，不负责 size / 精度 / 下单执行。需上游成交回调推进状态。

from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Optional, Dict, Any, Deque, Tuple


class ActionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"   # 保留类型以便状态查询时使用


@dataclass
class StrategyConfig:
    token_id: str
    buy_price_threshold: Optional[float] = None        # 触发买入的目标价格（可选）
    profit_ratio: float = 0.05                        # 兼容旧字段，默认 5%

    # 新增参数：基于窗口的跌幅/涨幅监控
    drop_window_minutes: float = 10.0
    drop_pct: float = 0.05
    profit_pct: Optional[float] = 0.05
    max_history_points: int = 600

    # 卖出后动态抬升跌幅阈值（默认启用）
    enable_incremental_drop_pct: bool = True
    incremental_drop_pct_step: float = 0.01
    incremental_drop_pct_cap: float = 0.20

    # 轻量防抖：同一方向的“待确认”状态下不重复发信号
    disable_duplicate_signal: bool = True

    # 可选价域守门（避免极端边界价误触）
    min_price: Optional[float] = 0.0
    max_price: Optional[float] = 1.0


@dataclass
class Action:
    action: ActionType
    token_id: str
    reason: str
    ref_price: float                 # 触发时参考的行情价：BUY 用 best_ask，SELL 用 best_bid
    target_price: Optional[float] = None  # SELL 时为 entry * (1 + profit_pct)
    extra: Dict[str, Any] = field(default_factory=dict)


class VolArbStrategy:
    """
    极简策略状态机（单 token）——严格“确认后换态”版：
      - FLAT → 当 best_ask <= buy_price_threshold 时，发出 BUY；
      - LONG → 当 best_bid >= entry_price * (1 + profit_ratio) 时，发出 SELL。

    注：
      * 本策略不处理 size/精度/下单，只产生信号，由上游执行。
      * 发出 BUY/SELL 信号后进入“待确认”状态，必须由上游在成交后调用
        on_buy_filled / on_sell_filled 才会推进状态机；on_reject() 解除待确认。
    """

    def __init__(self, config: StrategyConfig):
        self.cfg = config
        # profit_pct 与旧字段 profit_ratio 对齐
        if self.cfg.profit_pct is None:
            self.cfg.profit_pct = self.cfg.profit_ratio
        else:
            self.cfg.profit_ratio = self.cfg.profit_pct

        self._state: str = "FLAT"  # or "LONG"
        self._entry_price: Optional[float] = None
        self._awaiting: Optional[ActionType] = None  # BUY/SELL
        self._last_signal: Optional[ActionType] = None
        self._position_size: Optional[float] = None

        # 价格历史缓存：[(timestamp, price)]
        self._price_history: Deque[Tuple[float, float]] = deque()
        self._history_window_seconds: float = self.cfg.drop_window_minutes * 60.0

        # 跌幅统计
        self._window_high_price: Optional[float] = None
        self._window_low_price: Optional[float] = None
        self._max_drop_ratio: Optional[float] = None
        self._current_drop_ratio: Optional[float] = None

        # 最近行情记录
        self._last_tick_ts: Optional[float] = None
        self._last_best_ask: Optional[float] = None
        self._last_best_bid: Optional[float] = None

        # 状态字段
        self._last_buy_price: Optional[float] = None
        self._last_sell_price: Optional[float] = None
        self._manual_stop: bool = False
        self._manual_stop_reason: Optional[str] = None
        self._last_reject_reason: Optional[str] = None

        # 记录跌幅阈值的初始值（用于动态递增的下限）
        self._initial_drop_pct: float = max(self.cfg.drop_pct, 0.0)

    # ------------------------ 上游主调用：每笔行情快照 ------------------------
    def on_tick(
        self,
        best_ask: float,
        best_bid: float,
        ts: Optional[float] = None,
    ) -> Optional[Action]:
        """
        上游每次行情推送调用。返回 Action（BUY/SELL）或 None（无动作）。
        """
        if ts is None:
            ts = time.time()

        # 价域守门（如不需要可在 cfg 设置为 None）
        if self.cfg.min_price is not None and (best_ask < self.cfg.min_price or best_bid < self.cfg.min_price):
            return None
        if self.cfg.max_price is not None and (best_ask > self.cfg.max_price or best_bid > self.cfg.max_price):
            return None

        self._last_tick_ts = ts
        self._last_best_ask = best_ask
        self._last_best_bid = best_bid

        price_for_drop = self._prepare_price_history(ts, (best_bid + best_ask) / 2)

        if self._manual_stop:
            return None

        if self._state == "FLAT":
            return self._maybe_buy(price_for_drop, best_ask, ts)

        elif self._state == "LONG":
            return self._maybe_sell(best_bid, ts)

        return None

    # ------------------------ 买入/卖出触发判定 ------------------------
    def _maybe_buy(self, drop_price: float, best_ask: float, ts: Optional[float]) -> Optional[Action]:
        if self._awaiting == ActionType.BUY and self.cfg.disable_duplicate_signal:
            return None  # 等待上游确认，不重复发 BUY

        drop_trigger = False
        drop_ratio: Optional[float] = None
        window_high: Optional[float] = self._window_high_price

        if len(self._price_history) > 1 and window_high is not None and window_high > 0:
            drop_ratio = (window_high - drop_price) / window_high
            drop_trigger = drop_ratio >= self.cfg.drop_pct

        threshold_trigger = (
            self.cfg.buy_price_threshold is not None
            and best_ask <= self.cfg.buy_price_threshold
        )

        if not drop_trigger and not threshold_trigger:
            return None

        reasons = []
        extra = {
            "history_points": len(self._price_history),
            "drop_window_minutes": self.cfg.drop_window_minutes,
            "drop_triggered": drop_trigger,
            "threshold_triggered": threshold_trigger,
        }


        if drop_trigger and drop_ratio is not None and window_high is not None:
            reasons.append(
                f"drop({drop_ratio:.4f}) ≥ threshold({self.cfg.drop_pct:.4f}) from high({window_high:.5f})"
            )
            extra.update(
                {
                    "drop_ratio": drop_ratio,
                    "window_high": window_high,
                    "drop_price": drop_price,
                }
            )

        if threshold_trigger and self.cfg.buy_price_threshold is not None:
            reasons.append(
                f"best_ask({best_ask:.5f}) ≤ buy_threshold({self.cfg.buy_price_threshold:.5f})"
            )

        act = Action(
            action=ActionType.BUY,
            token_id=self.cfg.token_id,
            reason="; ".join(reasons) or "drop trigger",
            ref_price=best_ask,
            extra=extra,
        )
        self._last_signal = ActionType.BUY
        self._awaiting = ActionType.BUY  # 必须等待上游 on_buy_filled() 确认
        return act

    def _maybe_sell(self, best_bid: float, ts: Optional[float]) -> Optional[Action]:
        if self._entry_price is None:
            return None  # 防守式检查

        if self._awaiting == ActionType.SELL and self.cfg.disable_duplicate_signal:
            return None  # 等待上游确认，不重复发 SELL

        profit_pct = self.cfg.profit_pct if self.cfg.profit_pct is not None else self.cfg.profit_ratio
        target = self._entry_price * (1.0 + profit_pct)
        gain_ratio: Optional[float] = None
        if self._entry_price > 0:
            gain_ratio = (best_bid - self._entry_price) / self._entry_price

        if best_bid >= target:
            reason = (
                f"best_bid({best_bid:.5f}) ≥ target({target:.5f}) = entry({self._entry_price:.5f}) * (1+{profit_pct:.4f})"
            )
            extra = {
                "gain_ratio": gain_ratio,
                "profit_pct": profit_pct,
            }
            act = Action(
                action=ActionType.SELL,
                token_id=self.cfg.token_id,
                reason=reason,
                ref_price=best_bid,
                target_price=target,
                extra=extra,
            )
            self._last_signal = ActionType.SELL
            self._awaiting = ActionType.SELL  # 必须等待上游 on_sell_filled() 确认
            return act
        return None

    def _prepare_price_history(self, ts: float, price: float) -> float:
        self._price_history.append((ts, price))
        self._trim_history(ts)
        return price

    def _trim_history(self, ts: float) -> None:
        window = self._history_window_seconds
        while self._price_history and ts - self._price_history[0][0] > window:
            self._price_history.popleft()
        while self._price_history and len(self._price_history) > self.cfg.max_history_points:
            self._price_history.popleft()
        if self._price_history:
            self._update_drop_metrics()
        else:
            self._reset_drop_metrics()

    def _reset_drop_metrics(self) -> None:
        self._window_high_price = None
        self._window_low_price = None
        self._max_drop_ratio = None
        self._current_drop_ratio = None

    def _update_drop_metrics(self) -> None:
        if not self._price_history:
            self._reset_drop_metrics()
            return

        high_price: Optional[float] = None
        low_price: Optional[float] = None
        for _, px in self._price_history:
            if high_price is None or px > high_price:
                high_price = px
            if low_price is None or px < low_price:
                low_price = px

        if high_price is None:
            self._reset_drop_metrics()
            return

        current_price = self._price_history[-1][1]
        if high_price > 0:
            max_drop = (
                (high_price - low_price) / high_price
                if low_price is not None and low_price <= high_price
                else 0.0
            )
            current_drop = (
                (high_price - current_price) / high_price
                if current_price is not None
                else None
            )
        else:
            max_drop = 0.0
            current_drop = 0.0 if current_price is not None else None

        self._window_high_price = high_price
        self._window_low_price = low_price
        self._max_drop_ratio = max_drop
        self._current_drop_ratio = current_drop

    # ------------------------ 上游回调：成交/被拒 ------------------------
    def on_buy_filled(self, avg_price: float, size: Optional[float] = None) -> None:
        """上游在实际买入成交后回调。"""
        if self._awaiting == ActionType.BUY:
            self._state = "LONG"
            self._entry_price = avg_price
            self._last_buy_price = avg_price
            if size is not None:
                self._position_size = float(size)
        self._awaiting = None
        self._last_reject_reason = None

    def on_sell_filled(self, avg_price: Optional[float] = None) -> None:
        """上游在实际卖出成交后回调。"""
        if self._awaiting == ActionType.SELL:
            self._state = "FLAT"
            self._entry_price = None
            self._position_size = None
            self._awaiting = None
        if self._state == "FLAT":
            # 记录卖出价
            if avg_price is not None:
                self._last_sell_price = avg_price
            else:
                # 如果当前 tick 有最新 best_bid 则优先使用
                self._last_sell_price = self._last_best_bid
            self._maybe_increment_drop_pct()
        self._last_reject_reason = None

    def on_reject(self, reason: Optional[str] = None) -> None:
        """上游在下单失败/被拒绝时回调，解除“待确认”以便重新发信号。"""
        self._awaiting = None
        self._last_reject_reason = reason

    def stop(self, reason: Optional[str] = None) -> None:
        """手动暂停策略或在市场关闭时调用。"""
        self._manual_stop = True
        self._manual_stop_reason = reason
        self._awaiting = None

    def resume(self) -> None:
        """恢复策略运行。"""
        self._manual_stop = False
        self._manual_stop_reason = None

    # ------------------------ 实用方法 ------------------------
    def update_params(
        self,
        *,
        buy_price_threshold: Optional[float] = None,
        profit_ratio: Optional[float] = None,
        drop_window_minutes: Optional[float] = None,
        drop_pct: Optional[float] = None,
        profit_pct: Optional[float] = None,
        max_history_points: Optional[int] = None,
        enable_incremental_drop_pct: Optional[bool] = None,
        incremental_drop_pct_step: Optional[float] = None,
        incremental_drop_pct_cap: Optional[float] = None,
    ) -> None:
        if buy_price_threshold is not None:
            self.cfg.buy_price_threshold = buy_price_threshold
        if profit_ratio is not None:
            self.cfg.profit_ratio = profit_ratio
            self.cfg.profit_pct = profit_ratio
        if profit_pct is not None:
            self.cfg.profit_pct = profit_pct
            self.cfg.profit_ratio = profit_pct
        if drop_window_minutes is not None:
            self.cfg.drop_window_minutes = drop_window_minutes
            self._history_window_seconds = drop_window_minutes * 60.0
            if self._last_tick_ts is not None:
                self._trim_history(self._last_tick_ts)
        if drop_pct is not None:
            self.cfg.drop_pct = drop_pct
            self._initial_drop_pct = max(drop_pct, 0.0)
        if max_history_points is not None:
            self.cfg.max_history_points = max(1, int(max_history_points))
            if self._last_tick_ts is not None:
                self._trim_history(self._last_tick_ts)
        if enable_incremental_drop_pct is not None:
            self.cfg.enable_incremental_drop_pct = bool(enable_incremental_drop_pct)
        if incremental_drop_pct_step is not None:
            self.cfg.incremental_drop_pct_step = float(incremental_drop_pct_step)
        if incremental_drop_pct_cap is not None:
            self.cfg.incremental_drop_pct_cap = float(incremental_drop_pct_cap)

    def sell_trigger_price(self) -> Optional[float]:
        if self._entry_price is None:
            return None
        profit_pct = self.cfg.profit_pct if self.cfg.profit_pct is not None else self.cfg.profit_ratio
        return self._entry_price * (1.0 + profit_pct)

    def status(self) -> Dict[str, Any]:
        return {
            "state": self._state,
            "awaiting": self._awaiting,
            "entry_price": self._entry_price,
            "sell_trigger": self.sell_trigger_price(),
            "position_size": self._position_size,
            "last_signal": self._last_signal,
            "last_buy_price": self._last_buy_price,
            "last_sell_price": self._last_sell_price,
            "price_history_len": len(self._price_history),
            "manual_stop": self._manual_stop,
            "manual_stop_reason": self._manual_stop_reason,
            "last_reject_reason": self._last_reject_reason,
            "last_tick": {
                "ts": self._last_tick_ts,
                "best_ask": self._last_best_ask,
                "best_bid": self._last_best_bid,
            },
            "drop_stats": {
                "window_high": self._window_high_price,
                "window_low": self._window_low_price,
                "max_drop_ratio": self._max_drop_ratio,
                "current_drop_ratio": self._current_drop_ratio,
                "window_seconds": self._history_window_seconds,
            },
            "config": {
                "token_id": self.cfg.token_id,
                "buy_price_threshold": self.cfg.buy_price_threshold,
                "profit_ratio": self.cfg.profit_ratio,
                "drop_window_minutes": self.cfg.drop_window_minutes,
                "drop_pct": self.cfg.drop_pct,
                "profit_pct": self.cfg.profit_pct,
                "max_history_points": self.cfg.max_history_points,
                "price_band": (self.cfg.min_price, self.cfg.max_price),
                "disable_duplicate_signal": self.cfg.disable_duplicate_signal,
                "enable_incremental_drop_pct": self.cfg.enable_incremental_drop_pct,
                "incremental_drop_pct_step": self.cfg.incremental_drop_pct_step,
                "incremental_drop_pct_cap": self.cfg.incremental_drop_pct_cap,
            },
        }

    # ------------------------ 内部辅助 ------------------------
    def _maybe_increment_drop_pct(self) -> None:
        if not getattr(self.cfg, "enable_incremental_drop_pct", False):
            return
        step = max(getattr(self.cfg, "incremental_drop_pct_step", 0.0), 0.0)
        if step <= 0:
            return
        current = max(self.cfg.drop_pct, self._initial_drop_pct)
        cap = getattr(self.cfg, "incremental_drop_pct_cap", None)
        if cap is not None:
            cap = max(cap, self._initial_drop_pct)
            current = min(current, cap)
            new_drop = min(current + step, cap)
        else:
            new_drop = current + step
        self.cfg.drop_pct = new_drop
