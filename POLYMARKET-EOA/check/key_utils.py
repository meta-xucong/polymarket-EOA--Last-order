#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享的 Polymarket 聚合 key 辅助函数。

`make_key` 将 conditionId 与 outcome/outcomeIndex 统一拼接，
用于 claim 与 positions 两个视角的对齐。
"""

from __future__ import annotations

from typing import Optional


def make_key(condition_id: Optional[str], outcome_index: Optional[int], outcome: Optional[str]) -> str:
    """生成统一的聚合 key：`conditionId#outcome`。

    - 优先使用 conditionId + outcome 文本。
    - 若 outcome 为空，则尝试 conditionId + outcomeIndex。
    - 若 conditionId 也缺失，则返回 `UNKNOWN#...` 作为降级聚合。
    """

    cid = (condition_id or "").strip()
    label = (outcome or "").strip()

    if not cid:
        fallback = label or (str(outcome_index) if outcome_index is not None else "UNK")
        return f"UNKNOWN#{fallback}"

    if label:
        return f"{cid}#{label}"

    if outcome_index is not None:
        return f"{cid}#{outcome_index}"

    return f"{cid}#UNK"


__all__ = ["make_key"]
