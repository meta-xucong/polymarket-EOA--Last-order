# Volatility_arbitrage_main_rest_EOA.py
# -*- coding: utf-8 -*-
"""
Polymarket CLOB API 连接器（EOA 版）。

与 Safe 方案相比，此版本直接使用 Externally Owned Account (EOA) 私钥完成签名，
需要在环境变量中提供私钥及对应的钱包地址：

必填环境变量（按优先顺序读取，找到即停）：
- 私钥：POLY_EOA_KEY / POLY_KEY
- 钱包地址：POLY_EOA_ADDRESS / POLY_ADDRESS（缺失时会尝试用私钥推导）

可选环境变量（带默认值）：
- POLY_HOST       : 默认 https://clob.polymarket.com
- POLY_CHAIN_ID   : 默认 137（Polygon）
- POLY_SIGNATURE  : 默认 0（EOA / EIP-712）

用法：
>>> from Volatility_arbitrage_main_rest_EOA import get_client
>>> client = get_client()  # 返回已设置 API credentials 的 ClobClient
"""

from __future__ import annotations

import os
from typing import Iterable, Optional, Tuple

from py_clob_client.client import ClobClient

# ---- 默认配置 ----
DEFAULT_HOST = "https://clob.polymarket.com"
DEFAULT_CHAIN_ID = 137
DEFAULT_SIGNATURE_TYPE = 0

_KEY_ENV_CANDIDATES = ("POLY_EOA_KEY", "POLY_KEY")

# 仅允许显式的 EOA 地址变量，彻底忽略历史遗留的 Safe 地址配置。
_EOA_ADDRESS_ENV_CANDIDATES = ("POLY_EOA_ADDRESS", "POLY_ADDRESS")

_CLIENT_SINGLETON: Optional[ClobClient] = None


def _api_creds_to_dict(creds) -> dict:
    """最大化从凭据对象中提取字段，以兼容多种返回结构。"""
    if creds is None:
        return {}
    if isinstance(creds, dict):
        return creds
    if hasattr(creds, "_asdict"):
        try:
            maybe_map = creds._asdict()  # type: ignore[attr-defined]
        except Exception:
            maybe_map = None
        else:
            if isinstance(maybe_map, dict):
                return maybe_map
    if hasattr(creds, "items"):
        try:
            maybe_map = dict(creds.items())  # type: ignore[arg-type]
        except Exception:
            maybe_map = None
        else:
            if isinstance(maybe_map, dict):
                return maybe_map
    if hasattr(creds, "to_dict"):
        try:
            maybe_map = creds.to_dict()  # type: ignore[attr-defined]
        except Exception:
            maybe_map = None
        else:
            if isinstance(maybe_map, dict):
                return maybe_map
    if hasattr(creds, "__dict__"):
        raw = vars(creds)
        if isinstance(raw, dict):
            public = {k: v for k, v in raw.items() if not k.startswith("_")}
            if public:
                return public
    # 兜底处理 __slots__ 等无 __dict__ 的对象。
    attrs = {}
    for name in dir(creds):
        if name.startswith("_"):
            continue
        try:
            value = getattr(creds, name)
        except Exception:
            continue
        if callable(value):
            continue
        attrs[name] = value
    return attrs


def _extract_api_field(creds, *names) -> Optional[str]:
    mp = _api_creds_to_dict(creds)
    for name in names:
        val = mp.get(name)
        if val:
            return str(val)
    if hasattr(creds, "__getitem__"):
        for name in names:
            try:
                val = creds[name]  # type: ignore[index]
            except Exception:
                continue
            if val:
                return str(val)
    for name in names:
        if hasattr(creds, name):
            try:
                val = getattr(creds, name)
            except Exception:
                continue
            if val:
                return str(val)
    # 针对序列结构（如 tuple/list）按约定顺序取值
    if isinstance(creds, (tuple, list)):
        index_map = {
            "key": 0,
            "api_key": 0,
            "apiKey": 0,
            "id": 0,
            "secret": 1,
            "apiSecret": 1,
            "passphrase": 2,
            "apiPassphrase": 2,
        }
        for name in names:
            idx = index_map.get(name)
            if idx is None:
                continue
            if idx < len(creds):
                try:
                    candidate = creds[idx]
                except Exception:
                    continue
                if candidate:
                    return str(candidate)
    return None


def _extract_api_key(creds) -> str:
    """从任意凭据结构中抽取 key 字段。"""
    key = _extract_api_field(creds, "key", "api_key", "apiKey", "id")
    return key if key else "<missing>"


def _normalize_privkey(key: str) -> str:
    if key.startswith(("0x", "0X")):
        return key[2:]
    return key


def _first_env(candidates: Iterable[str]) -> Tuple[str, str]:
    for name in candidates:
        val = os.getenv(name)
        if isinstance(val, str) and val.strip():
            return name, val.strip()
    joined = " or ".join(candidates)
    raise KeyError(f"缺少环境变量：{joined}")


def _derive_address_from_key(raw_key: str) -> Optional[str]:
    """根据私钥推导地址，用于缺少显式配置时兜底。"""
    try:
        from eth_account import Account  # type: ignore
    except Exception:
        return None

    key = raw_key if raw_key.startswith("0x") else f"0x{raw_key}"
    try:
        account = Account.from_key(key)
    except Exception:
        return None
    return getattr(account, "address", None)


def _select_eoa_address(raw_key: str) -> str:
    """优先使用显式的 EOA 地址配置，缺失时尝试从私钥推导。"""
    for name in _EOA_ADDRESS_ENV_CANDIDATES:
        val = os.getenv(name)
        if isinstance(val, str) and val.strip():
            cleaned = val.strip()
            if not cleaned.startswith(("0x", "0X")) and len(cleaned) == 40:
                cleaned = "0x" + cleaned
            return cleaned

    derived = _derive_address_from_key(raw_key)
    if derived:
        print("[WARN] 未显式提供 EOA 地址，已根据私钥推导并使用：%s" % derived)
        return derived

    joined = " or ".join(_EOA_ADDRESS_ENV_CANDIDATES)
    raise KeyError(f"缺少环境变量：{joined}")


def init_client() -> ClobClient:
    host = os.getenv("POLY_HOST", DEFAULT_HOST)
    chain_id = int(os.getenv("POLY_CHAIN_ID", str(DEFAULT_CHAIN_ID)))

    raw_key = _first_env(_KEY_ENV_CANDIDATES)[1]
    key = _normalize_privkey(raw_key)

    funder = _select_eoa_address(raw_key)

    env_signature = os.getenv("POLY_SIGNATURE")
    signature_type = DEFAULT_SIGNATURE_TYPE
    if env_signature and env_signature.strip():
        try:
            env_sig_val = int(env_signature.strip())
        except ValueError:
            print(
                "[WARN] 检测到无效的 POLY_SIGNATURE=%s，已回退为 %s (EOA / EIP-712)。"
                % (env_signature, DEFAULT_SIGNATURE_TYPE)
            )
        else:
            signature_type = env_sig_val
            if env_sig_val != DEFAULT_SIGNATURE_TYPE:
                print(
                    "[WARN] 检测到 POLY_SIGNATURE=%s，与 EOA 默认值 %s 不同，请确认是否符合预期。"
                    % (env_signature, DEFAULT_SIGNATURE_TYPE)
                )

    if os.getenv("POLY_FUNDER"):
        print("[WARN] 检测到 POLY_FUNDER 环境变量，EOA 模式已忽略该值。")

    client = ClobClient(
        host,
        key=key,
        chain_id=chain_id,
        signature_type=signature_type,
        funder=funder,
    )

    api_creds = client.create_or_derive_api_creds()
    client.set_api_creds(api_creds)
    try:
        setattr(client, "api_creds", api_creds)
    except Exception:
        pass

    return client


def get_client() -> ClobClient:
    global _CLIENT_SINGLETON
    if _CLIENT_SINGLETON is None:
        _CLIENT_SINGLETON = init_client()
    return _CLIENT_SINGLETON




def get_api_creds_tuple():
    """
    返回 (api_key, api_secret, api_passphrase_or_none)
    优先读取环境变量 POLY_API_KEY / POLY_API_SECRET / POLY_API_PASSPHRASE；
    若缺失则基于 EOA 私钥派生（不发网络请求）。
    """
    # 1) 环境变量优先
    env_key = os.getenv("POLY_API_KEY")
    env_secret = os.getenv("POLY_API_SECRET")
    env_pass = os.getenv("POLY_API_PASSPHRASE") or os.getenv("POLY_API_PASS") or None
    if env_key and env_secret:
        return str(env_key), str(env_secret), (str(env_pass) if env_pass else None)

    # 2) 基于单例 client 生成/读取
    c = get_client()
    creds = getattr(c, "api_creds", None)
    # 兼容多种返回结构
    def _from_creds(obj):
        if obj is None:
            return None
        key = _extract_api_key(obj)
        sec = _extract_api_field(obj, "secret", "apiSecret")
        pas = _extract_api_field(obj, "passphrase", "apiPassphrase")
        if key != "<missing>" and sec:
            return (str(key), str(sec), (str(pas) if pas else None))
        return None
    tup = _from_creds(creds)
    if tup:
        return tup
    # 3) 兜底：直接派生一次
    fresh = c.create_or_derive_api_creds()
    c.set_api_creds(fresh)
    try:
        setattr(c, "api_creds", fresh)
    except Exception:
        pass
    tup = _from_creds(fresh)
    if tup:
        return tup
    return (None, None, None)


if __name__ == "__main__":
    client = get_client()
    api_creds = getattr(client, "api_creds", None)
    api_key = _extract_api_key(api_creds)
    print("[CHECK] API credentials key=%s..." % api_key)
