# Polymarket_onetime_approval_EOA.py
# -*- coding: utf-8 -*-
"""一次性链上授权工具（EOA 版）。

此脚本用于在首次连接 Polymarket CLOB 前完成链上授权：
- 对 USDC（ERC-20）设置足够的 allowance；
- 对 Conditional Tokens（ERC-1155）执行 setApprovalForAll。

授权目标优先通过 ``/onboarding/config`` 接口自动获取；
若接口不可访问，可通过环境变量 ``POLY_APPROVAL_TARGETS``
提供 JSON 配置，或依靠脚本内置的默认主网地址。

运行前请确保：
- 已在环境变量中提供 EOA 私钥与地址（同 ``Volatility_arbitrage_main_rest_EOA``）；
- 环境变量 ``POLY_RPC_URL`` 指向可用的 Polygon RPC（默认 https://polygon-rpc.com）。

示例：
>>> python Polymarket_onetime_approval_EOA.py
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, List, Optional, Tuple

import requests
from eth_account.signers.local import LocalAccount
from hexbytes import HexBytes
from web3 import Web3
from web3.contract import Contract
from web3.middleware import geth_poa_middleware

from Volatility_arbitrage_main_rest_EOA import (
    DEFAULT_CHAIN_ID,
    DEFAULT_HOST,
    _KEY_ENV_CANDIDATES,
    _first_env,
    _normalize_privkey,
    _select_eoa_address,
)

DEFAULT_RPC_URL = "https://polygon-rpc.com"
DEFAULT_PRIORITY_FEE = int(os.getenv("POLY_PRIORITY_FEE", "30000000000"))  # 30 gwei
MAX_ALLOWANCE = 2**256 - 1
# 默认授权金额（单位：USDC），可通过环境变量 POLY_APPROVAL_USDC 覆盖。
DEFAULT_APPROVAL_USDC = Decimal(os.getenv("POLY_APPROVAL_USDC", "1000"))


def _default_erc20_amount(decimals: int) -> int:
    scaled = DEFAULT_APPROVAL_USDC * (Decimal(10) ** int(decimals))
    return int(scaled)

DEFAULT_USDC_ADDRESS = os.getenv(
    "POLY_USDC_ADDRESS",
    "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
)
DEFAULT_CONDITIONAL_TOKENS_ADDRESS = os.getenv(
    "POLY_CONDITIONAL_TOKENS_ADDRESS",
    "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
)
DEFAULT_CTF_EXCHANGE = os.getenv(
    "POLY_CTF_EXCHANGE_ADDRESS",
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
)



DEFAULT_NEGRISK_EXCHANGE = os.getenv(
    "POLY_NEGRISK_EXCHANGE_ADDRESS",
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",
)
DEFAULT_NEGRISK_ADAPTER = os.getenv(
    "POLY_NEGRISK_ADAPTER_ADDRESS",
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
)
class ApprovalKind:
    ERC20 = "erc20"
    ERC1155 = "erc1155"


@dataclass
class ApprovalTarget:
    name: str
    token_address: str
    spender_address: str
    kind: str = ApprovalKind.ERC20
    amount: Optional[int] = None
    decimals: Optional[int] = None

    def normalized_amount(self, default: int = MAX_ALLOWANCE) -> int:
        if self.kind != ApprovalKind.ERC20:
            return default
        if self.amount is None:
            return default
        normalized = int(self.amount)
        if normalized > 0:
            return normalized
        if self.decimals is not None:
            return _default_erc20_amount(self.decimals)
        return default


ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
]

ERC1155_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


def _to_checksum(w3: Web3, address: str) -> str:
    return w3.to_checksum_address(address)


def _fetch_remote_targets(host: str) -> List[ApprovalTarget]:
    url = host.rstrip("/") + "/onboarding/config"
    try:
        resp = requests.get(url, timeout=10)
    except Exception as exc:  # pragma: no cover - 网络依赖
        print(f"[WARN] 无法访问 {url}: {exc}")
        return []
    if resp.status_code != 200:
        print(f"[WARN] 获取 {url} 返回状态码 {resp.status_code}")
        return []
    try:
        data = resp.json()
    except Exception as exc:  # pragma: no cover - 网络依赖
        print(f"[WARN] 解析 {url} JSON 失败: {exc}")
        return []

    targets: List[ApprovalTarget] = []
    allowances = data.get("allowances") or []
    for entry in allowances:
        try:
            token_addr = entry.get("token", {}).get("address") or entry["token"]
            spender = entry.get("spender") or entry["contract"]
        except Exception:
            continue
        amount_val = entry.get("amount")
        decimals = entry.get("token", {}).get("decimals")
        name = entry.get("label") or f"Allowance {token_addr}->{spender}"
        amt = None
        if amount_val is not None:
            try:
                amt = int(str(amount_val), 0)
            except ValueError:
                try:
                    amt = int(Decimal(str(amount_val)))
                except Exception:
                    amt = None
        targets.append(
            ApprovalTarget(
                name=name,
                token_address=str(token_addr),
                spender_address=str(spender),
                kind=ApprovalKind.ERC20,
                amount=amt,
                decimals=int(decimals) if decimals is not None else None,
            )
        )

    approvals = data.get("approvals") or data.get("operators") or []
    for entry in approvals:
        try:
            token_addr = entry.get("token", {}).get("address") or entry["token"]
            operator = entry.get("operator") or entry.get("spender")
        except Exception:
            continue
        name = entry.get("label") or f"Operator {token_addr}->{operator}"
        targets.append(
            ApprovalTarget(
                name=name,
                token_address=str(token_addr),
                spender_address=str(operator),
                kind=ApprovalKind.ERC1155,
            )
        )

    return targets


def _load_env_targets(raw: str) -> List[ApprovalTarget]:
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise ValueError("POLY_APPROVAL_TARGETS 不是合法 JSON") from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("POLY_APPROVAL_TARGETS 需为数组或对象")
    targets: List[ApprovalTarget] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"POLY_APPROVAL_TARGETS[{idx}] 不是对象")
        try:
            target = ApprovalTarget(
                name=str(item.get("name") or f"Target#{idx}"),
                token_address=str(item["token_address"]),
                spender_address=str(item["spender_address"]),
                kind=str(item.get("kind", ApprovalKind.ERC20)).lower(),
                amount=(int(item["amount"]) if "amount" in item else None),
            )
        except KeyError as exc:
            raise ValueError(f"POLY_APPROVAL_TARGETS[{idx}] 缺少字段 {exc}") from exc
        targets.append(target)
    return targets


def _fallback_targets() -> List[ApprovalTarget]:
    return [
        ApprovalTarget(
            name="USDC -> Conditional Tokens",
            token_address=DEFAULT_USDC_ADDRESS,
            spender_address=DEFAULT_CONDITIONAL_TOKENS_ADDRESS,
            kind=ApprovalKind.ERC20,
            amount=_default_erc20_amount(6),
            decimals=6,
        ),
        ApprovalTarget(
            name="USDC -> CTF Exchange",
            token_address=DEFAULT_USDC_ADDRESS,
            spender_address=DEFAULT_CTF_EXCHANGE,
            kind=ApprovalKind.ERC20,
            amount=_default_erc20_amount(6),
            decimals=6,
        ),
        
        ApprovalTarget(
            name="USDC -> NegRisk Exchange",
            token_address=DEFAULT_USDC_ADDRESS,
            spender_address=DEFAULT_NEGRISK_EXCHANGE,
            kind=ApprovalKind.ERC20,
            amount=_default_erc20_amount(6),
            decimals=6,
        ),
        ApprovalTarget(
            name="USDC -> NegRisk Adapter",
            token_address=DEFAULT_USDC_ADDRESS,
            spender_address=DEFAULT_NEGRISK_ADAPTER,
            kind=ApprovalKind.ERC20,
            amount=_default_erc20_amount(6),
            decimals=6,
        ),
        ApprovalTarget(
            name="Conditional Tokens setApprovalForAll",
            token_address=DEFAULT_CONDITIONAL_TOKENS_ADDRESS,
            spender_address=DEFAULT_CTF_EXCHANGE,
            kind=ApprovalKind.ERC1155,
        ),
    ]


def load_targets(host: str) -> List[ApprovalTarget]:
    env_json = os.getenv("POLY_APPROVAL_TARGETS")
    if env_json:
        print("[INFO] 使用 POLY_APPROVAL_TARGETS 中的自定义授权配置。")
        return _load_env_targets(env_json)

    remote_targets = _fetch_remote_targets(host)
    if remote_targets:
        print("[INFO] 已从远端获取授权清单，共 %d 项。" % len(remote_targets))
        return remote_targets

    print("[WARN] 无法从远端获取授权清单，使用内置默认。")
    return _fallback_targets()


def _connect_web3(rpc_url: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise RuntimeError(f"无法连接到 RPC：{rpc_url}")
    w3.middleware_onion.inject(geth_poa_middleware, layer=0)
    return w3


def _resolve_account() -> Tuple[LocalAccount, str]:
    key_env, raw_key = _first_env(_KEY_ENV_CANDIDATES)
    print(f"[INFO] 使用环境变量 {key_env} 提供的私钥。")
    normalized = _normalize_privkey(raw_key)
    w3 = Web3()
    account: LocalAccount = w3.eth.account.from_key(normalized)
    return account, raw_key


def _ensure_owner_address(account: LocalAccount, raw_key: str) -> str:
    explicit = _select_eoa_address(raw_key)
    checksum = Web3.to_checksum_address(explicit)
    if checksum.lower() != account.address.lower():
        print(
            "[WARN] 环境中的地址与私钥推导地址不一致：env=%s signer=%s" %
            (explicit, account.address)
        )
        print("[WARN] 将以签名地址 %s 发送交易。" % account.address)
    return account.address


def _human_amount(amount: int, decimals: Optional[int]) -> str:
    if decimals is None:
        return str(amount)
    scaled = Decimal(amount) / (Decimal(10) ** int(decimals))
    return f"{scaled.normalize()} ({amount})"


def _prepare_fee_fields(w3: Web3) -> dict:
    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas")
    priority = DEFAULT_PRIORITY_FEE
    if base_fee is None:
        gas_price = w3.eth.gas_price or priority
        return {"gasPrice": int(gas_price)}
    max_fee = int(base_fee + priority * 2)
    return {
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority,
    }


def _send_transaction(
    w3: Web3,
    account: LocalAccount,
    call,
    base_tx: dict,
    description: str,
) -> HexBytes:
    tx = call.build_transaction(base_tx)
    if "gas" not in tx:
        estimate = w3.eth.estimate_gas({**tx, "from": account.address})
        tx["gas"] = int(estimate * 1.1)
    fee_fields = _prepare_fee_fields(w3)
    tx.update(fee_fields)
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    print(f"[INFO] 已发送交易 {description}: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    status = receipt.get("status")
    if status != 1:
        raise RuntimeError(f"交易 {tx_hash.hex()} 执行失败，status={status}")
    print(f"[INFO] 交易成功 {tx_hash.hex()}，gasUsed={receipt.get('gasUsed')}")
    return tx_hash


def ensure_approval(w3: Web3, account: LocalAccount, target: ApprovalTarget, chain_id: int, nonce: int) -> int:
    token_addr = _to_checksum(w3, target.token_address)
    spender = _to_checksum(w3, target.spender_address)
    base_tx = {"from": account.address, "nonce": nonce, "chainId": chain_id}

    if target.kind == ApprovalKind.ERC20:
        contract: Contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        allowance = contract.functions.allowance(account.address, spender).call()
        required = target.normalized_amount()
        symbol = None
        try:
            symbol = contract.functions.symbol().call()
        except Exception:
            symbol = "ERC20"
        if allowance >= required:
            print(
                f"[SKIP] {target.name} 已满足，当前 allowance={allowance} >= 需求 {required}."
            )
            return nonce
        print(
            f"[ACTION] {target.name}: allowance {allowance} -> {required}"
            f" ({_human_amount(required, target.decimals)})."
        )
        _send_transaction(
            w3,
            account,
            contract.functions.approve(spender, required),
            base_tx,
            f"approve {symbol} -> {spender}",
        )
        return nonce + 1

    if target.kind == ApprovalKind.ERC1155:
        contract = w3.eth.contract(address=token_addr, abi=ERC1155_ABI)
        approved = contract.functions.isApprovedForAll(account.address, spender).call()
        if approved:
            print(f"[SKIP] {target.name} 已 setApprovalForAll。")
            return nonce
        print(f"[ACTION] {target.name}: setApprovalForAll -> True")
        _send_transaction(
            w3,
            account,
            contract.functions.setApprovalForAll(spender, True),
            base_tx,
            f"setApprovalForAll {token_addr} -> {spender}",
        )
        return nonce + 1

    raise ValueError(f"未知的授权类型：{target.kind}")


def main(argv: Optional[Iterable[str]] = None) -> None:
    host = os.getenv("POLY_HOST", DEFAULT_HOST)
    rpc_url = os.getenv("POLY_RPC_URL", DEFAULT_RPC_URL)
    chain_id = int(os.getenv("POLY_CHAIN_ID", str(DEFAULT_CHAIN_ID)))

    print(f"[INFO] 目标主机：{host}")
    print(f"[INFO] 使用 RPC：{rpc_url}")
    print(f"[INFO] Chain ID：{chain_id}")

    account, raw_key = _resolve_account()
    owner_address = _ensure_owner_address(account, raw_key)
    print(f"[INFO] 签名地址：{owner_address}")

    w3 = _connect_web3(rpc_url)
    print(f"[INFO] 已连接 RPC，最新区块：{w3.eth.block_number}")

    targets = load_targets(host)
    if not targets:
        raise RuntimeError("未找到任何授权目标，请检查配置。")

    nonce = w3.eth.get_transaction_count(account.address)
    print(f"[INFO] 当前 nonce = {nonce}")

    for target in targets:
        print("-" * 72)
        try:
            nonce = ensure_approval(w3, account, target, chain_id, nonce)
        except Exception as exc:
            print(f"[ERROR] 处理 {target.name} 失败：{exc}")
            raise

    print("-" * 72)
    print("[DONE] 所有授权流程已完成。")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[FATAL] {exc}")
        sys.exit(1)
