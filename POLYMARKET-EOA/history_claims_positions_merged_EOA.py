#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""融合 claim 与历史持仓的聚合脚本（EOA 版）。

完整使用流程：
1) 先生成历史成交 / 仓位 JSON（默认会写到脚本同目录）：
    python3 history_positions_summary_EOA.py --json positions.json
2) 再生成 claim JSON（可指定筛选时间）：
    python3 history_claims_summary_EOA.py --json claims.json --since "2024-10-01"
3) 最后运行本脚本做融合与导出：
    python3 history_claims_positions_merged_EOA.py \
        --positions-json positions.json \
        --claims-json claims.json \
        --json-out merged.json \
        --xlsx-out merged.xlsx
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set

UTC_PLUS_8 = timezone(timedelta(hours=8))


def _condition_id_of(entry: Dict[str, Any]) -> str:
    return str(
        entry.get("conditionId")
        or entry.get("condition_id")
        or entry.get("conditionID")
        or ""
    ).strip()


def _is_placeholder_key(key: str) -> bool:
    text = str(key or "")
    return text.startswith("UNKNOWN#") or text.endswith("#UNK") or text.endswith("#999")


def _canonicalize_key(
    raw_key: str, condition_id: str, cond_to_pos_keys: Dict[str, Set[str]]
) -> str:
    key = str(raw_key or "")
    if _is_placeholder_key(key) and condition_id:
        pos_keys = cond_to_pos_keys.get(condition_id)
        if pos_keys and len(pos_keys) == 1:
            return next(iter(pos_keys))
    return key


def _load_json(path: str, flag: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(
            "未找到文件：{}\n"
            "请先运行：\n"
            "  python3 history_positions_summary_EOA.py --json positions.json\n"
            "  python3 history_claims_summary_EOA.py --json claims.json [--since YYYY-MM-DD]\n"
            "并用 --{flag.replace('_', '-')} 指向生成的文件。".format(path, flag=flag)
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(UTC_PLUS_8)
    except Exception:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M")


def _merge_positions_and_claims(
    positions_payload: Dict[str, Any], claims_payload: Dict[str, Any]
) -> List[Dict[str, Any]]:
    pos_by_key = {
        str(p.get("key")): p
        for p in positions_payload.get("positions", [])
        if isinstance(p, dict) and p.get("key")
    }
    cond_to_pos_keys: Dict[str, Set[str]] = {}
    for key, pos in pos_by_key.items():
        cid = _condition_id_of(pos)
        if cid:
            cond_to_pos_keys.setdefault(cid, set()).add(key)

    claim_pos_by_key: Dict[str, Dict[str, Any]] = {}
    for p in (claims_payload.get("positions") or {}).values():
        if not (isinstance(p, dict) and p.get("key")):
            continue
        cid = _condition_id_of(p)
        canonical_key = _canonicalize_key(p.get("key"), cid, cond_to_pos_keys)
        normalized = dict(p)
        normalized["key"] = canonical_key
        existing = claim_pos_by_key.get(canonical_key)
        if existing:
            for field in (
                "buy_size_total",
                "buy_cost_total",
                "sell_size_total",
                "sell_proceeds_total",
                "redeem_size_total",
                "redeem_usdc_total",
            ):
                existing[field] = float(existing.get(field, 0.0)) + float(
                    normalized.get(field, 0.0)
                )
            for meta_field in ("title", "slug", "outcome", "side"):
                if not existing.get(meta_field) and normalized.get(meta_field):
                    existing[meta_field] = normalized.get(meta_field)
        else:
            claim_pos_by_key[canonical_key] = normalized

    claim_events: List[Dict[str, Any]] = []
    for e in claims_payload.get("claim_events", []):
        if not isinstance(e, dict):
            continue
        cid = _condition_id_of(e)
        canonical_key = _canonicalize_key(e.get("key"), cid, cond_to_pos_keys)
        normalized = dict(e)
        normalized["key"] = canonical_key
        claim_events.append(normalized)

    all_keys = sorted(set(pos_by_key) | set(claim_pos_by_key))
    merged: List[Dict[str, Any]] = []

    for key in all_keys:
        base = pos_by_key.get(key)
        claim_pos = claim_pos_by_key.get(key)
        src = base or claim_pos or {}
        title = src.get("title") or src.get("marketSlug") or ""
        slug = src.get("slug") or src.get("marketSlug") or ""
        outcome = src.get("outcome") or src.get("tokenOutcomeLabel") or ""
        side = src.get("side") or src.get("tokenOutcomeSide")

        buy_size = (base or {}).get("buy_size_total")
        buy_cost = (base or {}).get("buy_cost_total")
        sell_size = (base or {}).get("sell_size_total")
        sell_proceeds = (base or {}).get("sell_proceeds_total")

        if buy_size is None:
            buy_size = (claim_pos or {}).get("buy_size_total", 0.0)
        if buy_cost is None:
            buy_cost = (claim_pos or {}).get("buy_cost_total", 0.0)
        if sell_size is None:
            sell_size = (claim_pos or {}).get("sell_size_total", 0.0)
        if sell_proceeds is None:
            sell_proceeds = (claim_pos or {}).get("sell_proceeds_total", 0.0)

        redeem_size = (claim_pos or {}).get("redeem_size_total", 0.0)
        redeem_usdc = (claim_pos or {}).get("redeem_usdc_total", 0.0)

        net_cash_flow = -float(buy_cost or 0.0) + float(sell_proceeds or 0.0) + float(
            redeem_usdc or 0.0
        )

        claim_ts_list = [
            e.get("timestamp") for e in claim_events if str(e.get("key")) == key
        ]
        claim_ts_list = [float(ts) for ts in claim_ts_list if ts is not None]
        first_claim_ts = min(claim_ts_list) if claim_ts_list else None
        last_claim_ts = max(claim_ts_list) if claim_ts_list else None

        merged.append(
            {
                "key": key,
                "title": title,
                "slug": slug,
                "outcome": outcome,
                "side": side,
                "buy_size_total": float(buy_size or 0.0),
                "buy_cost_total": float(buy_cost or 0.0),
                "sell_size_total": float(sell_size or 0.0),
                "sell_proceeds_total": float(sell_proceeds or 0.0),
                "redeem_size_total": float(redeem_size or 0.0),
                "redeem_usdc_total": float(redeem_usdc or 0.0),
                "net_cash_flow": net_cash_flow,
                "has_claim": bool(claim_ts_list) or float(redeem_usdc or 0.0) > 0,
                "has_position_info": base is not None,
                "first_claim_ts": first_claim_ts,
                "last_claim_ts": last_claim_ts,
            }
        )

    merged.sort(key=lambda r: (r.get("last_claim_ts") or 0, r.get("key")), reverse=True)
    return merged


def _print_merged(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("[INFO] 没有可用的数据，请确认 JSON 输入是否正确。")
        return
    print("\n[MERGED] claim × 历史持仓 汇总：")
    for idx, row in enumerate(rows, 1):
        key = row.get("key")
        cash_flow = row.get("net_cash_flow") or 0.0
        print(
            f"{idx:>3}. {row.get('title') or '-'} | {row.get('outcome') or '-'} | key={key}"
        )
        print(
            "     "
            f"买入成本≈{row.get('buy_cost_total', 0.0):.2f} | 卖出收入≈{row.get('sell_proceeds_total', 0.0):.2f} | redeem≈{row.get('redeem_usdc_total', 0.0):.2f}"
        )
        print(
            "     "
            f"净现金流≈{cash_flow:.2f} | has_position={row.get('has_position_info')} | has_claim={row.get('has_claim')}"
        )
        print(
            "     "
            f"claim 时间={_fmt_ts(row.get('first_claim_ts'))} -> {_fmt_ts(row.get('last_claim_ts'))}"
        )
        if str(key).startswith("UNKNOWN#"):
            print("     [NOTE] outcome 缺失，只能按 market 级别粗聚合。")
        print()


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return

    settled_rows = [r for r in rows if r.get("has_claim")]
    unsettled_rows = [r for r in rows if not r.get("has_claim")]

    total_entries = len(rows)
    settled_entries = len(settled_rows)
    unsettled_entries = len(unsettled_rows)

    total_invest = sum(float(r.get("buy_cost_total") or 0.0) for r in settled_rows)
    total_profit = sum(float(r.get("net_cash_flow") or 0.0) for r in settled_rows)
    success_count = sum(
        1 for r in settled_rows if float(r.get("net_cash_flow") or 0.0) >= 0
    )
    failure_count = settled_entries - success_count
    roi = (total_profit / total_invest * 100) if total_invest > 0 else 0.0

    print("[SUMMARY] 统计概览：")
    print(
        f"总条目={total_entries} | 已结算={settled_entries} | 未结算={unsettled_entries}"
    )
    print(f"命中={success_count} | 失利={failure_count}（仅统计已结算）")
    print(
        "总投入≈{:.2f} | 总收益≈{:.2f} | 总收益率≈{:.2f}%（仅已结算）".format(
            total_invest, total_profit, roi
        )
    )


def _export_json(rows: List[Dict[str, Any]], path: str) -> None:
    payload = {"merged": rows}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[INFO] 已写入 JSON：{path}")


def _export_xlsx(rows: List[Dict[str, Any]], path: str) -> None:
    try:
        from openpyxl import Workbook
    except Exception as exc:  # pragma: no cover - 可选依赖
        print(f"[WARN] 未安装 openpyxl，无法导出 Excel：{exc}")
        return

    wb = Workbook()
    ws = wb.active
    ws.title = "merged"
    headers = [
        "key",
        "title",
        "outcome",
        "side",
        "buy_size_total",
        "buy_cost_total",
        "sell_size_total",
        "sell_proceeds_total",
        "redeem_size_total",
        "redeem_usdc_total",
        "net_cash_flow",
        "has_claim",
        "has_position_info",
        "first_claim_ts",
        "last_claim_ts",
        "first_claim_at_utc8",
        "last_claim_at_utc8",
    ]
    ws.append(headers)
    for row in rows:
        ws.append(
            [
                row.get("key"),
                row.get("title"),
                row.get("outcome"),
                row.get("side"),
                row.get("buy_size_total"),
                row.get("buy_cost_total"),
                row.get("sell_size_total"),
                row.get("sell_proceeds_total"),
                row.get("redeem_size_total"),
                row.get("redeem_usdc_total"),
                row.get("net_cash_flow"),
                row.get("has_claim"),
                row.get("has_position_info"),
                row.get("first_claim_ts"),
                row.get("last_claim_ts"),
                _fmt_ts(row.get("first_claim_ts")),
                _fmt_ts(row.get("last_claim_ts")),
            ]
        )
    wb.save(path)
    print(f"[INFO] 已写入 Excel：{path}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="融合 claim 与历史仓位汇总")
    parser.add_argument(
        "--positions-json",
        default=os.path.join(os.path.dirname(__file__), "positions.json"),
        help="history_positions_summary_EOA.py --json 的输出路径",
    )
    parser.add_argument(
        "--claims-json",
        default=os.path.join(os.path.dirname(__file__), "claims.json"),
        help="history_claims_summary_EOA.py --json 的输出路径",
    )
    parser.add_argument("--json-out", help="将融合结果导出为 JSON")
    parser.add_argument("--xlsx-out", help="将融合结果导出为 Excel")
    args = parser.parse_args(argv)

    try:
        positions_payload = _load_json(args.positions_json, "positions_json")
    except Exception as exc:
        print(f"[ERR] 读取 positions JSON 失败：{exc}", file=sys.stderr)
        return 2
    try:
        claims_payload = _load_json(args.claims_json, "claims_json")
    except Exception as exc:
        print(f"[ERR] 读取 claims JSON 失败：{exc}", file=sys.stderr)
        return 3

    merged_rows = _merge_positions_and_claims(positions_payload, claims_payload)
    _print_merged(merged_rows)
    _print_summary(merged_rows)

    if args.json_out:
        _export_json(merged_rows, args.json_out)
    if args.xlsx_out:
        _export_xlsx(merged_rows, args.xlsx_out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
