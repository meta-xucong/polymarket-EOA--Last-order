
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket EOA 一键 Claim（覆盖版 · Data-API + 自动识别 USDC.e/WCOL + 自动 unwrap）
更新要点：即使当次没有可兑付市场，也会继续执行“自动 unwrap WCOL → USDC.e”。
"""
import os, sys, json, argparse
from typing import Any, Dict, List, Optional, Set, Tuple

from web3 import Web3
from eth_account import Account

# ---------- 常量（可用环境变量覆盖） ----------
POLYGON_RPC = os.environ.get("POLYGON_RPC", "https://polygon-rpc.com")
CTF_ADDRESS = Web3.to_checksum_address(os.environ.get("CTF_ADDRESS", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"))  # ConditionalTokens@Polygon
USDCe_ADDRESS = Web3.to_checksum_address(os.environ.get("USDCe_ADDRESS", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"))
WCOL_ADDRESS  = Web3.to_checksum_address(os.environ.get("WCOL_ADDRESS",  "0x3A3BD7bb9528E159577F7C2e685CC81A765002E2"))  # NegRisk Wrapped Collateral
ZERO32 = "0x" + "00"*32
DATA_API_BASE = os.environ.get("POLY_DATA_API", "https://data-api.polymarket.com")

CLAIM_ABI = [
    {"name":"getCollectionId","type":"function","stateMutability":"pure",
     "inputs":[{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSet","type":"uint256"}],
     "outputs":[{"name":"collectionId","type":"bytes32"}]},
    {"name":"getPositionId","type":"function","stateMutability":"pure",
     "inputs":[{"name":"collateralToken","type":"address"},{"name":"collectionId","type":"bytes32"}],
     "outputs":[{"name":"positionId","type":"uint256"}]},
    {"name":"balanceOf","type":"function","stateMutability":"view",
     "inputs":[{"name":"account","type":"address"},{"name":"id","type":"uint256"}],
     "outputs":[{"name":"balance","type":"uint256"}]},
    {"name":"balanceOfBatch","type":"function","stateMutability":"view",
     "inputs":[{"name":"accounts","type":"address[]"},{"name":"ids","type":"uint256[]"}],
     "outputs":[{"name":"balances","type":"uint256[]"}]},
    {"name":"redeemPositions","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],
     "outputs":[]},
]

ERC20_ABI = [
    {"name":"balanceOf","type":"function","stateMutability":"view",
     "inputs":[{"name":"account","type":"address"}],
     "outputs":[{"name":"balance","type":"uint256"}]},
    {"name":"decimals","type":"function","stateMutability":"view","inputs":[],"outputs":[{"name":"","type":"uint8"}]},
    {"name":"symbol","type":"function","stateMutability":"view","inputs":[],"outputs":[{"name":"","type":"string"}]},
]

WCOL_ABI = ERC20_ABI + [
    {"name":"unwrap","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"_to","type":"address"},{"name":"_amount","type":"uint256"}],"outputs":[]},
]


def _looks_like_panic_overflow(exc: Exception) -> bool:
    """
    兜底识别 Solidity Panic(0x11)：“算术运算溢出/下溢”。
    web3 抛出的异常结构不固定，这里只要匹配 revert data 0x4e487b71...0011 即视为命中。
    """

    text = repr(exc)
    if not text:
        return False
    text = text.lower()
    return "0x4e487b71" in text and "0011" in text

# ---------- 小工具 ----------
def _b32(x: str) -> str:
    x = str(x)
    if x.startswith("0x") and len(x)==66:
        return x
    raise ValueError(f"bad bytes32: {x}")

def _norm_b32(x: str) -> str:
    try:    return _b32(x)
    except: return ZERO32

def _derive_position_ids(contract, parent: str, condition: str, index_sets: List[int], collateral: str) -> List[int]:
    ids: List[int] = []
    p = _b32(parent); c = _b32(condition)
    col_addr = Web3.to_checksum_address(collateral)
    for idx in index_sets:
        col = contract.functions.getCollectionId(p, c, int(idx)).call()
        pid = int(contract.functions.getPositionId(col_addr, col).call())
        ids.append(pid)
    return ids

def _choose_collateral(contract, owner: str, parent: str, condition: str, index_sets: List[int], known_token_ids: Set[int], prefer_usdce_first=True) -> Tuple[Optional[str], List[int], List[int]]:
    """返回 (chosen_collateral, position_ids, balances)；若均不命中则 (None, [], [])"""
    usdce = USDCe_ADDRESS
    wcol  = WCOL_ADDRESS
    order = [usdce, wcol] if prefer_usdce_first else [wcol, usdce]

    # 1) 先用 token_id 直接命中（若数据源提供了 tokenIds，则更快更准）
    for col in order:
        try:
            ids = _derive_position_ids(contract, parent, condition, index_sets, col)
            if known_token_ids and any(pid in known_token_ids for pid in ids):
                bals = contract.functions.balanceOfBatch([owner]*len(ids), ids).call()
                return col, ids, list(map(int, bals))
        except Exception:
            pass

    # 2) 再用余额>0 命中
    for col in order:
        try:
            ids = _derive_position_ids(contract, parent, condition, index_sets, col)
            bals = contract.functions.balanceOfBatch([owner]*len(ids), ids).call()
            if sum(int(x) for x in bals) > 0:
                return col, ids, list(map(int, bals))
        except Exception:
            pass

    return None, [], []

# ---------- Data-API 拉取可兑付分组 ----------
def fetch_claims_from_data_api(owner: str, only_redeemable: bool=True, limit: int=500) -> List[Dict[str, Any]]:
    """
    返回的每项：{
      "title": str,
      "conditionId": str,
      "parentCollectionId": "0x"+"00"*32,
      "indexSets": [1,2],
      "tokenIds": []   # Data-API 默认不回 tokenId，这里留空即可
    }
    """
    import requests
    q = {"user": owner, "limit": str(limit)}
    if only_redeemable:
        q["redeemable"] = "true"
    url = f"{DATA_API_BASE}/positions"
    r = requests.get(url, params=q, timeout=20)
    r.raise_for_status()
    arr = r.json()

    groups: Dict[Tuple[str,str], Dict[str, Any]] = {}
    for it in arr if isinstance(arr, list) else []:
        cond = it.get("conditionId")
        if not cond:
            continue
        parent = ZERO32  # 二元市场常见为 0x0；缺失则默认 0x0
        key = (cond, parent)
        g = groups.get(key)
        if not g:
            g = {
                "title": it.get("title",""),
                "conditionId": cond,
                "parentCollectionId": parent,
                "indexSets": [1,2],
                "tokenIds": [],
            }
            groups[key] = g
    return list(groups.values())

# ---------- 可选：从本地 JSON 加载分组 ----------
def load_claims_from_json(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        arr = json.load(f)
    if not isinstance(arr, list):
        raise ValueError("claims json must be a list")
    out: List[Dict[str, Any]] = []
    for it in arr:
        out.append({
            "title": it.get("title",""),
            "conditionId": it["conditionId"],
            "parentCollectionId": it.get("parentCollectionId", ZERO32),
            "indexSets": it.get("indexSets", [1,2]),
            "tokenIds": it.get("tokenIds", []),
        })
    return out

# ---------- WCOL 自动 unwrap ----------
def unwrap_wcol_all(w3: Web3, owner: str, priv: str, gas_price: int, nonce: int, min_amount_units: int = 0) -> Tuple[int, Optional[str]]:
    """
    若 owner 的 WCOL 余额 >= min_amount_units，则一次性 unwrap 到 USDC.e，返回 (nonce_delta, txh_hex|None)
    """
    wcol = w3.eth.contract(address=WCOL_ADDRESS, abi=WCOL_ABI)
    bal = int(wcol.functions.balanceOf(owner).call())
    try:
        dec = int(wcol.functions.decimals().call())
    except Exception:
        dec = 6  # 兜底为 6
    try:
        sym = wcol.functions.symbol().call()
    except Exception:
        sym = "WCOL"

    if bal <= 0 or bal < int(min_amount_units):
        print(f"[INFO] WCOL 余额 {bal}（最小单位，{sym} 小数={dec}），无需 unwrap。")
        return 0, None

    # 预执行 unwrap
    call = wcol.functions.unwrap(owner, bal)
    try:
        call.call({"from": owner})
    except Exception as e:
        print(f"[WARN] unwrap 预执行失败：{e}; 放弃 unwrap。")
        return 0, None

    tx = call.build_transaction({"from": owner, "nonce": nonce, "gasPrice": gas_price})
    try:
        est = w3.eth.estimate_gas(tx)
        tx["gas"] = int(est * 1.2)
    except Exception:
        tx["gas"] = 180000

    signed = w3.eth.account.sign_transaction(tx, private_key=priv)
    txh = w3.eth.send_raw_transaction(signed.rawTransaction)
    rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=120)
    print(f"[INFO] WCOL unwrap 成功 {txh.hex()} | gasUsed={rcpt.gasUsed} | amount={bal} (最小单位)")
    return 1, txh.hex()

# ---------- 主流程 ----------
def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default="", help="你的 EOA 地址（不填则从私钥推断）")
    ap.add_argument("--from-json", default="", help="本地 claims.json，绕过 Data-API")
    ap.add_argument("--gas-mult", type=float, default=1.0, help="gas price 放大倍数")
    ap.add_argument("--no-unwrap", action="store_true", help="禁用自动 WCOL → USDC.e unwrap")
    ap.add_argument("--unwrap-min", type=float, default=0.0, help="小于该 WCOL 数量（单位=枚/人类可读）不 unwrap，用于防尘，如 0.000001")
    args = ap.parse_args(argv)

    priv = os.environ.get("POLY_EOA_KEY") or os.environ.get("PRIVATE_KEY")
    if not priv:
        print("[FATAL] 缺少私钥环境变量 POLY_EOA_KEY / PRIVATE_KEY")
        return 2
    acct = Account.from_key(priv)
    owner = Web3.to_checksum_address(args.address or acct.address)
    print(f"[INFO] 使用钱包地址：{owner}")

    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    if not w3.is_connected():
        print("[FATAL] 连接 RPC 失败")
        return 3
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CLAIM_ABI)

    # 无论是否有可兑付市场，都先准备 nonce / gas，用于后续 unwrap 或 claim
    nonce = w3.eth.get_transaction_count(owner)
    gas_price = int(w3.eth.gas_price * max(float(args.gas_mult), 0.1))

    # 若有可兑付，先执行 claim；否则仅提示但不退出，让后续 unwrap 生效
    claim_error: Optional[Exception] = None
    claim_failures = 0
    try:
        # 准备分组
        if args.from_json:
            groups = load_claims_from_json(args.from_json)
        else:
            groups = fetch_claims_from_data_api(owner, only_redeemable=True, limit=500)

        if groups:
            print(f"[INFO] 即将处理 {len(groups)} 笔市场 Claim。")
            sent = 0
            for i, g in enumerate(groups, 1):
                title = g.get("title","")
                condition = _norm_b32(g.get("conditionId", ZERO32))
                parent = _norm_b32(g.get("parentCollectionId", ZERO32))
                index_sets = g.get("indexSets") or [1,2]
                token_ids = g.get("tokenIds", [])

                print("-"*72)
                print(f"[{i:02d}] 市场：{title}")
                print(f"[{i:02d}] 参数：parent={parent} | condition={condition} | indexSets={index_sets}")

                known_token_ids: Set[int] = set()
                for t in token_ids:
                    try: known_token_ids.add(int(t))
                    except: pass

                # 选择正确 collateral 并干跑余额校验
                col, cand_ids, bals = _choose_collateral(ctf, owner, parent, condition, index_sets, known_token_ids, prefer_usdce_first=True)
                if not col:
                    print("[SKIP] 干跑校验：USDC.e/WCOL 均为 0，跳过。")
                    continue

                have = sum(int(x) for x in bals)
                print(f"[TRACE] collateral={col} | positionIds={cand_ids} | balances={bals}")
                if have <= 0:
                    print("[SKIP] 干跑校验：余额为 0，跳过。")
                    continue

                # 预执行保护
                call = ctf.functions.redeemPositions(col, parent, condition, index_sets)
                try:
                    call.call({"from": owner})
                except Exception as e:
                    print(f"[WARN] 预执行失败：{e}; 跳过。")
                    continue

                # 上链
                try:
                    tx = call.build_transaction({"from": owner, "nonce": nonce, "gasPrice": gas_price})
                    try:
                        est = w3.eth.estimate_gas(tx)
                        tx["gas"] = int(est * 1.2)
                    except Exception:
                        tx["gas"] = 200000
                    signed = w3.eth.account.sign_transaction(tx, private_key=priv)
                    txh = w3.eth.send_raw_transaction(signed.rawTransaction)
                    rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=120)
                    print(f"[INFO] 交易成功 {txh.hex()} | gasUsed={rcpt.gasUsed}")
                    nonce += 1
                    sent += 1
                except Exception as exc:
                    claim_failures += 1
                    err_lower = str(exc).lower()
                    if "result for condition not received yet" in err_lower:
                        print(
                            "[WARN] redeemPositions 交易失败：预言机结果尚未提交，市场未结算。继续处理下一笔。"
                        )
                    elif _looks_like_panic_overflow(exc):
                        print(
                            f"[WARN] redeemPositions 被链上拒绝（Panic 0x11，可能已兑付完毕）：{exc}."
                            " 继续处理下一笔。"
                        )
                    else:
                        print(f"[WARN] redeemPositions 交易失败：{exc}。继续处理下一笔。")
                    # nonce 只有在交易成功广播并返回 receipt 时才增加
                    continue

            print("-"*72)
            print(f"[DONE] Claim 流程结束，发送交易 {sent} 笔。")
            if claim_failures:
                print(f"[WARN] 其中 {claim_failures} 笔在链上被拒绝或已无可兑付余额，请核查上方日志。")
        else:
            print("[INFO] 没有可兑付的市场（Data-API / 本地输入为空）。继续尝试自动 unwrap WCOL。")
    except Exception as e:
        claim_error = e
        print(f"[ERR] Claim 流程异常：{e}。将继续尝试自动 unwrap WCOL。")

    # 自动 unwrap（总是执行，除非被显式关闭）
    if not args.no_unwrap:
        # 用 WCOL 的 decimals 计算最小阈值（最小单位）
        wcol = w3.eth.contract(address=WCOL_ADDRESS, abi=WCOL_ABI)
        try:
            dec = int(wcol.functions.decimals().call())
        except Exception:
            dec = 6
        min_units = int(float(args.unwrap_min) * (10 ** dec))
        try:
            delta, txh = unwrap_wcol_all(
                w3, owner, priv, gas_price, nonce, min_amount_units=min_units
            )
            nonce += delta
        except Exception as exc:
            print(f"[WARN] 自动 unwrap 流程异常：{exc}。")
            # unwrap 异常不影响主流程退出码，但会在日志中提示

    if claim_error:
        print("[INFO] Claim 流程已执行：退出码 -1。")
        return -1

    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
