#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键 Claim 已结算仓位（EOA 版）。

本脚本在直接使用 EOA 的模式下，自动查找并 claim Polymarket 上所有
可领取的市场。脚本尽量复用仓库中既有的工具：

- 通过 :mod:`view_positions_EOA` 读取 Data-API 的 /positions 数据；
- 通过 :mod:`Polymarket_onetime_approval_EOA` 中的 Web3 连接与签名工具
  发送 ``redeemPositions`` 交易。

运行前请准备：

1. 环境变量中提供 EOA 私钥以及对应地址（同 ``Volatility_arbitrage_main_rest_EOA``）；
2. ``POLY_RPC_URL`` 指向可用的 Polygon RPC（默认 ``https://polygon-rpc.com``）；
3. ``requests``、``web3``、``eth_account`` 已安装。

可选参数：

``--dry-run``
    仅列出可 claim 市场，不发送交易。
``--min-usd``
    过滤掉预计 Claim 金额低于该阈值的市场（默认 0）。
``--max-txs``
    限制最多发送多少笔链上交易。

示例：

>>> python claim_all_markets_EOA.py --dry-run
>>> python claim_all_markets_EOA.py --min-usd 5
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from hexbytes import HexBytes
from web3 import Web3

try:  # 仓库内脚本，默认与本文件放在同一目录
    from view_positions_EOA import _fetch_positions_eoa, _infer_wallet_address
except Exception as exc:  # pragma: no cover - 导入失败时直接提示
    raise SystemExit(
        "[FATAL] 无法导入 view_positions_EOA，请确认依赖已安装。"
    ) from exc


# 直接调用 Data-API（redeemable=true）只拿可领取的仓位
import requests
DATA_API_HOST = os.environ.get("DATA_API_HOST", "https://data-api.polymarket.com").rstrip("/")

def _fetch_positions_redeemable(user_addr: str):
    url = f"{DATA_API_HOST}/positions"
    params = {
        "user": user_addr,
        "sizeThreshold": 0,
        "limit": 500,
        "redeemable": "true",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return data
from Polymarket_onetime_approval_EOA import (
    DEFAULT_CHAIN_ID,
    DEFAULT_CONDITIONAL_TOKENS_ADDRESS,
    DEFAULT_RPC_URL,
    DEFAULT_USDC_ADDRESS,
    _connect_web3,
    _ensure_owner_address,
    _resolve_account,
    _send_transaction,
)


# ``redeemPositions`` 最小 ABI
CLAIM_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "name": "balanceOfBatch",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "accounts", "type": "address[]"},
            {"name": "ids", "type": "uint256[]"},
        ],
        "outputs": [
            {"name": "balances", "type": "uint256[]"},
        ],
    },
]


ZERO_BYTES32 = HexBytes(b"\x00" * 32)


@dataclass
class ClaimPosition:
    """Data-API 中可 Claim 仓位的关键信息。"""

    position_id: str
    token_id: str
    outcome: str
    size: float
    claimable_shares: float
    claimable_value: float
    collateral_token: str
    parent_collection_id: str
    condition_id: str
    index_set: int
    market_title: str = ""
    market_slug: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def short_summary(self) -> str:
        return (
            f"token={self.token_id} outcome={self.outcome or '-'} "
            f"shares={self.claimable_shares:.4f} value≈${self.claimable_value:.2f} "
            f"indexSet={self.index_set}"
        )


@dataclass
class ClaimGroup:
    """将相同 condition 的仓位合并为一次 redeem 交易。"""

    collateral_token: str
    parent_collection_id: str
    condition_id: str
    positions: List[ClaimPosition] = field(default_factory=list)
    total_claimable_value: float = 0.0
    total_claimable_shares: float = 0.0

    def add(self, position: ClaimPosition) -> None:
        self.positions.append(position)
        self.total_claimable_value += position.claimable_value
        self.total_claimable_shares += position.claimable_shares

    @property
    def index_sets(self) -> List[int]:
        uniq = sorted({p.index_set for p in self.positions if p.index_set > 0})
        return uniq

    @property
    def display_title(self) -> str:
        for pos in self.positions:
            if pos.market_title:
                return pos.market_title
        return self.condition_id

    @property
    def display_slug(self) -> str:
        for pos in self.positions:
            if pos.market_slug:
                return pos.market_slug
        return ""

    def describe(self, prefix: str = "[TASK]") -> None:
        slug = self.display_slug
        slug_part = f" slug={slug}" if slug else ""
        print(
            f"{prefix} 市场：{self.display_title}{slug_part}"
        )
        print(
            f"{prefix} 预计领取：shares={self.total_claimable_shares:.4f}"
            f" | value≈${self.total_claimable_value:.2f}"
        )
        print(
            f"{prefix} 参数：collateral={self.collateral_token}"
            f" | conditionId={self.condition_id}"
            f" | parentCollectionId={self.parent_collection_id}"
            f" | indexSets={self.index_sets}"
        )
        for pos in self.positions:
            print(f"{prefix}   - {pos.short_summary()}")


def _coerce_float(value: Any) -> float:
    if value in (None, "", False):
        return 0.0
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    try:
        text = str(value).strip()
    except Exception:
        return 0.0
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        try:
            return float(Decimal(text))
        except Exception:
            return 0.0


def _coerce_int(value: Any) -> Optional[int]:
    if value in (None, "", False):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):  # 允许诸如 1.0
        try:
            return int(value)
        except Exception:
            return None
    if isinstance(value, Decimal):
        try:
            return int(value)
        except Exception:
            return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None
    try:
        if text.startswith(("0x", "0X")):
            return int(text, 16)
        return int(text)
    except ValueError:
        return None


def _truthy(value: Any) -> bool:
    if value in (None, ""):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "1", "claimable"}:
        return True
    if text in {"false", "no", "n", "0"}:
        return False
    return bool(text)


def _first_non_empty(*candidates: Any) -> Optional[str]:
    for cand in candidates:
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
        if cand and not isinstance(cand, (str, bytes, bytearray)):
            try:
                text = str(cand)
            except Exception:
                continue
            if text.strip():
                return text.strip()
    return None


def _normalize_bytes32(value: Any) -> HexBytes:
    if value in (None, "", 0, "0"):
        return ZERO_BYTES32
    if isinstance(value, (bytes, bytearray, HexBytes)):
        try:
            return HexBytes(value)
        except Exception:
            pass
    try:
        text = str(value).strip()
    except Exception:
        return ZERO_BYTES32
    if not text:
        return ZERO_BYTES32
    if text.startswith("0x"):
        body = text[2:]
    elif text.startswith("0X"):
        body = text[2:]
    else:
        body = text
    body = body.zfill(64)
    return HexBytes("0x" + body[-64:])


def _ensure_checksum(address: str) -> str:
    if not address:
        raise ValueError("空地址不可转换为 checksum")
    if not address.startswith("0x"):
        address = "0x" + address
    return Web3.to_checksum_address(address)


def _extract_from_market(raw: Dict[str, Any], *paths: str) -> Optional[Any]:
    market = raw.get("market") or {}
    for path in paths:
        if isinstance(market, dict) and path in market and market[path]:
            return market[path]
    return None


def _lookup_index_set_from_market(raw: Dict[str, Any], token_id: str) -> Optional[int]:
    market = raw.get("market") or {}
    candidates: Iterable[Any] = []
    if isinstance(market, dict):
        for key in ("outcomeTokens", "outcome_tokens", "outcomes"):
            maybe = market.get(key)
            if isinstance(maybe, list) and maybe:
                candidates = maybe
                break
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        entry_token = _first_non_empty(
            entry.get("tokenId"),
            entry.get("token_id"),
            entry.get("id"),
            entry.get("token_id_hex"),
        )
        if entry_token and token_id and entry_token.lower() == token_id.lower():
            idx = _coerce_int(
                entry.get("indexSet")
                or entry.get("index_set")
                or entry.get("index")
                or entry.get("outcomeIndex")
            )
            if idx is not None:
                if idx > 0:
                    return idx
                if idx == 0:
                    return 1
            outcome_idx = _coerce_int(entry.get("outcome"))
            if outcome_idx is not None and outcome_idx >= 0:
                return 1 << outcome_idx
    return None


def _deduce_index_set(raw: Dict[str, Any], token_id: str) -> Optional[int]:
    direct = _coerce_int(
        raw.get("indexSet")
        or raw.get("index_set")
        or raw.get("claimIndexSet")
        or raw.get("redeemIndexSet")
    )
    if direct and direct > 0:
        return direct

    outcome_idx = _coerce_int(
        raw.get("outcomeIndex")
        or raw.get("outcome_index")
        or raw.get("outcomeTokenIndex")
    )
    if outcome_idx is not None and outcome_idx >= 0:
        return 1 << outcome_idx

    outcome_id = _coerce_int(raw.get("outcomeId") or raw.get("outcome_id"))
    if outcome_id is not None and outcome_id >= 0:
        if outcome_id in (0, 1):
            return 1 << outcome_id
        if outcome_id > 1:
            return 1 << outcome_id

    outcome_text = _first_non_empty(
        raw.get("outcome"),
        raw.get("outcome_name"),
        raw.get("side"),
    )
    if outcome_text:
        lowered = outcome_text.lower()
        mapping = {
            "yes": 2,
            "no": 1,
            "long": 2,
            "short": 1,
            "invalid": 4,
        }
        if lowered in mapping:
            return mapping[lowered]
        if lowered.isdigit():
            outcome_idx = int(lowered)
            if outcome_idx >= 0:
                return 1 << outcome_idx

    from_market = _lookup_index_set_from_market(raw, token_id)
    if from_market is not None:
        return from_market

    return None


def _extract_market_title(raw: Dict[str, Any]) -> str:
    return _first_non_empty(
        raw.get("title"),
        raw.get("question"),
        _extract_from_market(raw, "title"),
        _extract_from_market(raw, "question"),
        _extract_from_market(raw, "slug"),
    ) or ""


def _extract_market_slug(raw: Dict[str, Any]) -> str:
    return _first_non_empty(
        raw.get("eventSlug"),
        raw.get("slug"),
        _extract_from_market(raw, "slug"),
        _extract_from_market(raw, "eventSlug"),
    ) or ""


def _detect_condition_id(raw: Dict[str, Any]) -> Optional[str]:
    return _first_non_empty(
        raw.get("conditionId"),
        raw.get("condition_id"),
        _extract_from_market(raw, "conditionId"),
        _extract_from_market(raw, "condition_id"),
    )


def _detect_parent_collection(raw: Dict[str, Any]) -> str:
    parent = _first_non_empty(
        raw.get("parentCollectionId"),
        raw.get("parent_collection_id"),
        raw.get("collectionId"),
        raw.get("collection_id"),
        _extract_from_market(raw, "parentCollectionId"),
    )
    return parent or "0x0"


def _detect_collateral(raw: Dict[str, Any]) -> str:
    return _first_non_empty(
        raw.get("collateralAddress"),
        raw.get("collateralToken"),
        raw.get("collateral_token"),
        _extract_from_market(raw, "collateralAddress"),
        _extract_from_market(raw, "collateralToken"),
    ) or DEFAULT_USDC_ADDRESS


def _detect_token_id(raw: Dict[str, Any]) -> Optional[str]:
    token = _first_non_empty(
        raw.get("asset"),
        raw.get("tokenId"),
        raw.get("token_id"),
        raw.get("positionTokenId"),
    )
    return token


def _detect_position_id(raw: Dict[str, Any]) -> str:
    return _first_non_empty(raw.get("id"), raw.get("positionId"), raw.get("position_id")) or ""


def _extract_claim_numbers(raw: Dict[str, Any]) -> Tuple[float, float]:
    shares = _coerce_float(
        raw.get("claimableShares")
        or raw.get("claimable_shares")
        or raw.get("redeemableShares")
        or raw.get("claimableTokenAmount")
        or raw.get("claimable_amount_tokens")
    )
    value = _coerce_float(
        raw.get("claimableValue")
        or raw.get("claimableUsd")
        or raw.get("claimableAmount")
        or raw.get("claimable_amount_usd")
        or raw.get("claimablePayout")
    )
    return shares, value


def _is_position_claimable(raw: Dict[str, Any]) -> bool:
    # Data-API 直接给出 redeemable 布尔标识
    if _truthy(raw.get("isClaimable")) or _truthy(raw.get("claimable")) or _truthy(raw.get("redeemable")):
        return True
    shares, value = _extract_claim_numbers(raw)
    if shares > 0 or value > 0:
        return True
    nested = raw.get("claim") or raw.get("claimData") or raw.get("payout")
    if isinstance(nested, dict):
        if _truthy(nested.get("claimable")):
            return True
        shares_nested = _coerce_float(nested.get("shares") or nested.get("amount"))
        value_nested = _coerce_float(nested.get("value") or nested.get("payout"))
        if shares_nested > 0 or value_nested > 0:
            return True
    return False


def _build_claim_position(raw: Dict[str, Any]) -> Optional[ClaimPosition]:
    if not isinstance(raw, dict):
        return None
    if not _is_position_claimable(raw):
        return None

    token_id = _detect_token_id(raw)
    if not token_id:
        return None

    index_set = _deduce_index_set(raw, token_id)
    if not index_set:
        return None

    condition_id = _detect_condition_id(raw)
    if not condition_id:
        return None

    parent_collection_id = _detect_parent_collection(raw)
    collateral_token = _detect_collateral(raw)

    shares, value = _extract_claim_numbers(raw)

    size = max(0.0, _coerce_float(raw.get("size") or raw.get("positionSize")))
    shares = max(shares, 0.0)
    value = max(value, 0.0)

    if shares <= 0.0 and size > 0.0:
        shares = size

    mark_price_raw = _first_non_empty(
        raw.get("markPrice"),
        raw.get("mark_price"),
        raw.get("price"),
        raw.get("avg_price"),
    )
    mark_price = _coerce_float(mark_price_raw)
    has_mark_price = mark_price_raw not in (None, "")
    if mark_price < 0.0:
        mark_price = 0.0

    if value <= 0.0 and shares > 0.0:
        payout_hint = _coerce_float(
            raw.get("payout")
            or raw.get("claimablePayout")
            or raw.get("claimable_payout")
            or raw.get("payoutPerShare")
            or raw.get("payout_per_share")
        )
        if payout_hint > 0.0:
            value = shares * payout_hint
        elif has_mark_price:
            value = shares * mark_price
        else:
            value = shares

    outcome = _first_non_empty(raw.get("outcome"), raw.get("side"), raw.get("outcome_name")) or ""

    position_id = _detect_position_id(raw)

    return ClaimPosition(
        position_id=position_id,
        token_id=str(token_id),
        outcome=outcome,
        size=size,
        claimable_shares=shares,
        claimable_value=value,
        collateral_token=str(collateral_token),
        parent_collection_id=str(parent_collection_id),
        condition_id=str(condition_id),
        index_set=int(index_set),
        market_title=_extract_market_title(raw),
        market_slug=_extract_market_slug(raw),
        raw=raw,
    )


def collect_claim_positions(positions: Sequence[Dict[str, Any]]) -> List[ClaimPosition]:
    claimables: List[ClaimPosition] = []
    for entry in positions:
        pos = _build_claim_position(entry)
        if pos and (pos.claimable_shares > 0 or pos.claimable_value > 0):
            claimables.append(pos)
    return claimables


def _filter_positions_with_onchain_balance(
    positions: Sequence[ClaimPosition],
    wallet_addr: str,
    *,
    rpc_url: str,
    contract_address: str,
) -> List[ClaimPosition]:
    if not positions:
        return []

    try:
        checksum_wallet = _ensure_checksum(wallet_addr)
    except Exception as exc:
        print(
            "[WARN] 无法解析钱包地址 %s：%s，跳过链上余额校验。"
            % (wallet_addr, exc)
        )
        return list(positions)

    try:
        w3 = _connect_web3(rpc_url)
    except Exception as exc:
        print("[WARN] 无法连接 RPC(%s)：%s，按 Data-API 结果继续。" % (rpc_url, exc))
        return list(positions)

    contract = _build_contract(w3, contract_address)

    accounts: List[str] = []
    token_ids: List[int] = []
    for pos in positions:
        accounts.append(checksum_wallet)
        token_text = str(pos.token_id)
        try:
            token_ids.append(int(token_text, 0))
        except Exception:
            try:
                token_ids.append(int(token_text))
            except Exception as exc:
                print(
                    "[WARN] 无法解析 tokenId=%s：%s，跳过链上余额校验。"
                    % (token_text, exc)
                )
                return list(positions)

    try:
        balances = contract.functions.balanceOfBatch(accounts, token_ids).call()
    except Exception as exc:
        print("[WARN] balanceOfBatch 调用失败，按 Data-API 结果继续：%s" % exc)
        return list(positions)

    filtered: List[ClaimPosition] = []
    skipped: List[ClaimPosition] = []
    for pos, bal in zip(positions, balances):
        try:
            bal_int = int(bal)
        except Exception:
            bal_int = 0
        if bal_int > 0:
            filtered.append(pos)
        else:
            skipped.append(pos)

    if skipped:
        print(
            "[INFO] 检测到 %d 个仓位链上余额为 0，自动跳过。"
            % len(skipped)
        )
        for pos in skipped:
            print(
                "[SKIP] token=%s market=%s"
                % (pos.token_id, pos.market_title or pos.condition_id)
            )

    return filtered


def group_claims(
    positions: Sequence[ClaimPosition],
    min_usd: float = 0.0,
) -> List[ClaimGroup]:
    groups: Dict[Tuple[str, str, str], ClaimGroup] = {}
    for pos in positions:
        key = (
            pos.collateral_token.lower(),
            str(pos.parent_collection_id).lower(),
            str(pos.condition_id).lower(),
        )
        grp = groups.get(key)
        if grp is None:
            grp = ClaimGroup(
                collateral_token=pos.collateral_token,
                parent_collection_id=pos.parent_collection_id,
                condition_id=pos.condition_id,
            )
            groups[key] = grp
        grp.add(pos)

    usd_threshold = float(min_usd or 0.0)

    result: List[ClaimGroup] = []
    for grp in groups.values():
        if grp.total_claimable_shares <= 0 and grp.total_claimable_value <= 0:
            continue
        if grp.total_claimable_value < usd_threshold:
            continue
        if not grp.index_sets:
            continue
        result.append(grp)

    result.sort(
        key=lambda g: (
            round(g.total_claimable_value, 6),
            round(g.total_claimable_shares, 6),
        ),
        reverse=True,
    )
    return result


def _build_contract(w3: Web3, address: str):
    checksum = _ensure_checksum(address)
    return w3.eth.contract(address=checksum, abi=CLAIM_ABI)


def _print_overview(groups: Sequence[ClaimGroup]) -> None:
    if not groups:
        print("[INFO] 没有可 claim 的市场。")
        return
    print("[INFO] 即将处理 %d 笔市场 Claim。" % len(groups))
    total_value = sum(g.total_claimable_value for g in groups)
    total_shares = sum(g.total_claimable_shares for g in groups)
    print(
        "[INFO] 汇总：shares=%.4f | value≈$%.2f"
        % (total_shares, total_value)
    )
    for idx, grp in enumerate(groups, 1):
        print("-" * 72)
        grp.describe(prefix=f"[{idx:02d}]")
    print("-" * 72)


def _args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="自动 claim Polymarket 已结算仓位（EOA 模式）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅输出计划，不发送链上交易",
    )
    parser.add_argument(
        "--min-usd",
        type=float,
        default=0.0,
        help="过滤掉预估 value 低于该金额的市场",
    )
    parser.add_argument(
        "--max-txs",
        type=int,
        default=None,
        help="限制最多发送多少笔 redeemPositions 交易",
    )
    parser.add_argument(
        "--ct-address",
        type=str,
        default=os.getenv(
            "POLY_CONDITIONAL_TOKENS_ADDRESS",
            DEFAULT_CONDITIONAL_TOKENS_ADDRESS,
        ),
        help="ConditionalTokens 合约地址（默认读取环境变量或仓库默认值）",
    )
    parser.add_argument(
        "--rpc-url",
        type=str,
        default=os.getenv("POLY_RPC_URL", DEFAULT_RPC_URL),
        help="Polygon RPC URL",
    )
    parser.add_argument(
        "--chain-id",
        type=int,
        default=int(os.getenv("POLY_CHAIN_ID", str(DEFAULT_CHAIN_ID))),
        help="链 ID (默认 137)",
    )
    return parser


def _check_wallet_consistency(wallet_addr: str, signer_addr: str) -> None:
    if wallet_addr.lower() != signer_addr.lower():
        print(
            "[WARN] Data-API 钱包地址 (%s) 与签名地址 (%s) 不一致。"
            % (wallet_addr, signer_addr)
        )
        print("[WARN] 将以签名地址发起交易，请确认环境变量设置是否符合预期。")


def _execute_claims(
    groups: Sequence[ClaimGroup],
    *,
    rpc_url: str,
    chain_id: int,
    contract_address: str,
    max_txs: Optional[int],
) -> None:
    if not groups:
        print("[INFO] 无需发送交易。")
        return

    account, raw_key = _resolve_account()
    signer_address = _ensure_owner_address(account, raw_key)

    w3 = _connect_web3(rpc_url)
    print(
        "[INFO] 已连接 RPC：%s，当前区块高度=%s"
        % (rpc_url, w3.eth.block_number)
    )

    contract = _build_contract(w3, contract_address)
    nonce = w3.eth.get_transaction_count(account.address)
    print(f"[INFO] 当前 nonce={nonce}")

    sent = 0
    for idx, grp in enumerate(groups, 1):
        if max_txs is not None and sent >= max_txs:
            print(
                "[INFO] 已达到 --max-txs=%s 限制，剩余市场跳过。" % max_txs
            )
            break

        print("-" * 72)
        grp.describe(prefix=f"[SEND {idx:02d}]")

        collateral = _ensure_checksum(grp.collateral_token)
        parent = _normalize_bytes32(grp.parent_collection_id)
        condition = _normalize_bytes32(grp.condition_id)
        index_sets = grp.index_sets
        if not index_sets:
            print("[SKIP] indexSets 为空，跳过该市场。")
            continue

        call = contract.functions.redeemPositions(
            collateral,
            parent,
            condition,
            index_sets,
        )

        try:
            call.call({"from": account.address})
        except Exception as exc:
            print(
                "[WARN] 预执行 redeemPositions 失败 (call reverted)：%s" % exc
            )
            print("[WARN] 跳过该市场，避免消耗 gas。")
            continue

        base_tx = {
            "from": account.address,
            "nonce": nonce,
            "chainId": chain_id,
        }

        try:
            _send_transaction(
                w3,
                account,
                call,
                base_tx,
                description=(
                    f"redeemPositions cond={grp.condition_id} indexSets={index_sets}"
                ),
            )
        except Exception as exc:
            print(f"[ERROR] 发送 redeemPositions 失败：{exc}")
            continue

        nonce += 1
        sent += 1

    print("-" * 72)
    print("[DONE] Claim 流程结束，共发送 %d 笔交易。" % sent)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _args_parser()
    args = parser.parse_args(argv)

    wallet = _infer_wallet_address()
    if not wallet:
        print("[ERR] 无法确定钱包地址，请检查环境变量。", file=sys.stderr)
        return 2

    print(f"[INFO] 使用钱包地址：{wallet}")

    try:
        positions = _fetch_positions_redeemable(wallet)
    except Exception as exc:
        print(f"[ERR] 获取持仓失败：{exc}", file=sys.stderr)
        return 3

    claim_positions = collect_claim_positions(positions)
    claim_positions = _filter_positions_with_onchain_balance(
        claim_positions,
        wallet,
        rpc_url=args.rpc_url,
        contract_address=args.ct_address,
    )
    if not claim_positions:
        print("[INFO] 当前没有可 claim 的仓位。")
        return 0

    groups = group_claims(claim_positions, min_usd=float(args.min_usd or 0.0))

    if not groups:
        print("[INFO] 没有满足阈值的 claim 市场。")
        return 0

    _print_overview(groups)

    if args.dry_run:
        print("[INFO] --dry-run 模式，未发送任何交易。")
        return 0

    account, raw_key = _resolve_account()
    signer_address = _ensure_owner_address(account, raw_key)
    _check_wallet_consistency(wallet, signer_address)

    _execute_claims(
        groups,
        rpc_url=args.rpc_url,
        chain_id=int(args.chain_id),
        contract_address=args.ct_address,
        max_txs=args.max_txs,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
