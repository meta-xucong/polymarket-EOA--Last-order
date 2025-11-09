# Volatility_arbitrage_run.py
# -*- coding: utf-8 -*-
"""
运行入口（循环策略版）：
- 事件页 /event/<slug>：列出子问题并选择（与老版一致）。
- 新增：
  1) 交互输入：买入份数（留空按 $1 反推）、跌幅窗口/阈值、盈利百分比、可选买入触发价；
  2) 基于 `VolArbStrategy` 的循环状态机：跌幅触发买入 → 成交确认 → 盈利达标卖出；
  3) 成交回调推进状态机，可重复执行买卖循环；
  4) 支持 stop 指令和市场关闭检测，安全退出。
"""
from __future__ import annotations
import sys
import os
import time
import threading
import re
from queue import Queue, Empty
from typing import Dict, Any, Iterable, Tuple, List, Optional
from decimal import Decimal, ROUND_UP
import hashlib
import hmac
import json
import requests
from datetime import datetime, timezone
from urllib.parse import urlencode
from Volatility_arbitrage_strategy import (
    StrategyConfig,
    VolArbStrategy,
    ActionType,
    Action,
)
from Volatility_buy import execute_auto_buy  # BUY 规范化逻辑统一交由执行器实现
from Volatility_sell import execute_auto_sell
from trading.execution import ExecutionResult

# ========== 1) Client：优先 ws 版，回退 rest 版 ==========
def _get_client():
    try:
        from Volatility_arbitrage_main_ws import get_client  # 优先
        return get_client()
    except Exception as e1:
        try:
            from Volatility_arbitrage_main_rest import get_client  # 退回
            return get_client()
        except Exception as e2:
            print("[ERR] 无法导入 get_client：", e1, "|", e2)
            sys.exit(1)

# ========== 2) 保留 price_watch 的单市场解析函数（先尝试） ==========
try:
    from Volatility_arbitrage_price_watch import resolve_token_ids
except Exception as e:
    print("[ERR] 无法从 Volatility_arbitrage_price_watch 导入 resolve_token_ids：", e)
    sys.exit(1)

# ========== 3) 行情订阅（未动） ==========
try:
    from Volatility_arbitrage_main_ws import ws_watch_by_ids
except Exception as e:
    print("[ERR] 无法从 Volatility_arbitrage_main_ws 导入 ws_watch_by_ids：", e)
    sys.exit(1)

GAMMA_ROOT = "https://gamma-api.polymarket.com"
DATA_API_ROOT = "https://data-api.polymarket.com"

# ===== 旧版解析器（复刻 + 极小修正） =====
def _parse_yes_no_ids_literal(source: str) -> Tuple[Optional[str], Optional[str]]:
    parts = [x.strip() for x in source.split(",")]
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    return None, None

def _extract_event_slug(s: str) -> str:
    m = re.search(r"/event/([^/?#]+)", s)
    if m: return m.group(1)
    s = s.strip()
    if s and ("/" not in s) and ("?" not in s) and ("&" not in s):
        return s
    return ""


def _extract_market_slug(s: str) -> str:
    m = re.search(r"/market/([^/?#]+)", s)
    if m:
        return m.group(1)
    s = s.strip()
    if s and ("/" not in s) and ("?" not in s) and ("&" not in s):
        return s
    return ""


def _parse_timestamp(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        ts = float(val)
        if ts > 1e12:
            ts = ts / 1000.0
        return ts
    if isinstance(val, str):
        raw = val.strip()
        if not raw:
            return None
        try:
            ts = float(raw)
            if ts > 1e12:
                ts = ts / 1000.0
            return ts
        except ValueError:
            pass
        iso = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d",
        ):
            try:
                dt = datetime.strptime(raw, fmt)
                dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
    return None


def _coerce_positive_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if num > 0:
        return num
    return None


def _market_meta_from_obj(m: dict) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if not isinstance(m, dict):
        return meta
    meta["slug"] = m.get("slug") or m.get("marketSlug") or m.get("market_slug")
    meta["market_id"] = (
        m.get("marketId")
        or m.get("id")
        or m.get("market_id")
        or m.get("conditionId")
        or m.get("condition_id")
    )

    end_keys = (
        "endDate",
        "endTime",
        "closeTime",
        "closeDate",
        "closedTime",
        "expiry",
        "expirationTime",
    )
    for key in end_keys:
        ts = _parse_timestamp(m.get(key))
        if ts:
            meta["end_ts"] = ts
            break

    resolve_keys = (
        "resolvedTime",
        "resolutionTime",
        "resolveTime",
        "resolvedAt",
        "finalizationTime",
        "finalizedTime",
        "settlementTime",
    )
    for key in resolve_keys:
        ts = _parse_timestamp(m.get(key))
        if ts:
            meta["resolved_ts"] = ts
            break

    if "end_ts" not in meta and "resolved_ts" in meta:
        meta["end_ts"] = meta["resolved_ts"]

    for key in (
        "minimumOrderSize",
        "minimum_order_size",
        "minOrderSize",
        "min_order_size",
    ):
        size_val = _coerce_positive_float(m.get(key))
        if size_val is not None:
            meta["minimum_order_size"] = size_val
            break

    for key in (
        "minimumTickSize",
        "minimum_tick_size",
        "minTickSize",
        "min_tick_size",
        "tickSize",
    ):
        tick_val = _coerce_positive_float(m.get(key))
        if tick_val is not None:
            meta["minimum_tick_size"] = tick_val
            break

    meta["raw"] = m
    return meta


def _maybe_fetch_market_meta_from_source(source: str) -> Dict[str, Any]:
    slug = _extract_market_slug(source)
    if not slug:
        return {}
    m = _fetch_market_by_slug(slug)
    if m:
        return _market_meta_from_obj(m)
    return {}


def _market_has_ended(meta: Dict[str, Any], now: Optional[float] = None) -> bool:
    if not meta:
        return False
    if now is None:
        now = time.time()
    candidates: List[float] = []
    for key in ("resolved_ts", "end_ts"):
        ts = meta.get(key)
        if isinstance(ts, (int, float)):
            candidates.append(float(ts))
    if not candidates:
        return False
    return now >= min(candidates)


def _extract_position_size(status: Dict[str, Any]) -> float:
    if not isinstance(status, dict):
        return 0.0
    for key in ("position_size", "position", "size"):
        val = status.get(key)
        if val is None:
            continue
        try:
            size = float(val)
            if size > 0:
                return size
        except (TypeError, ValueError):
            continue
    return 0.0


def _resolve_client_host(client) -> str:
    candidates = [
        getattr(client, "host", None),
        getattr(client, "_host", None),
        getattr(client, "api_url", None),
        getattr(client, "base_url", None),
    ]
    for cand in candidates:
        if not cand:
            continue
        if isinstance(cand, str):
            host = cand.strip()
            if not host:
                continue
            if host.startswith("http://") or host.startswith("https://"):
                return host.rstrip("/")
            return f"https://{host.lstrip('/')}".rstrip("/")
    return os.getenv("POLY_HOST", "https://clob.polymarket.com").rstrip("/")


def _sign_payload(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    key = secret.encode("utf-8")
    payload = (timestamp + method.upper() + path + (body or "")).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _signed_request(
    client,
    method: str,
    path: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
):
    creds = _extract_api_creds(client)
    if not creds:
        raise RuntimeError("缺少 API Key/Secret，无法签名 HTTP 请求。")

    host = _resolve_client_host(client)
    query = ""
    if params:
        query = "?" + urlencode(params, doseq=True)
    url = f"{host}{path}{query}"

    body = ""
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":"))

    ts = str(int(time.time() * 1000))
    signature_path = f"{path}{query}" if query else path
    signature = _sign_payload(creds["secret"], ts, method.upper(), signature_path, body)

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": creds["key"],
        "X-API-Signature": signature,
        "X-API-Timestamp": ts,
    }

    request_fn = getattr(requests, method.lower())
    try:
        resp = request_fn(url, data=body or None, headers=headers, timeout=10)
    except Exception as exc:
        raise RuntimeError(f"请求 {url} 失败：{exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return resp.status_code, data


def _extract_positions_from_data_api_response(payload: Any) -> Optional[List[Dict[str, Any]]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
    return None


def _describe_payload_shape(raw: Any) -> str:
    try:
        if raw is None:
            return "None"
        if isinstance(raw, dict):
            keys = list(raw.keys())
            if not keys:
                return "dict(keys=∅)"
            preview = ", ".join(str(k) for k in keys[:5])
            if len(keys) > 5:
                preview += ", …"
            return f"dict(keys={preview})"
        if isinstance(raw, list):
            length = len(raw)
            if not raw:
                return "list(len=0)"
            first_type = type(raw[0]).__name__
            return f"list(len={length}, first={first_type})"
        if isinstance(raw, tuple):
            return f"tuple(len={len(raw)})"
        if isinstance(raw, set):
            return f"set(len={len(raw)})"
        return type(raw).__name__
    except Exception:
        return type(raw).__name__


def _preview_payload(raw: Any, limit: int = 160) -> str:
    try:
        if isinstance(raw, (dict, list)):
            text = json.dumps(raw, ensure_ascii=False)  # type: ignore[arg-type]
        else:
            text = str(raw)
    except Exception:
        text = repr(raw)
    text = text.replace("\n", " ")
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _extract_token_id_from_position(position: Dict[str, Any]) -> Optional[str]:
    if not isinstance(position, dict):
        return None
    token_keys = (
        "tokenId",
        "token_id",
        "token",
        "asset",
        "asset_id",
        "assetId",
        "clobTokenId",
    )
    for key in token_keys:
        val = position.get(key)
        if isinstance(val, dict):
            nested = (
                val.get("id")
                or val.get("tokenId")
                or val.get("token_id")
                or val.get("assetId")
                or val.get("asset_id")
            )
            if nested:
                return str(nested)
        elif val:
            return str(val)

    side = str(
        position.get("outcome")
        or position.get("token_side")
        or position.get("side")
        or position.get("direction")
        or ""
    ).upper()
    yes_id = position.get("yesToken") or position.get("yes_token") or position.get("yesTokenId")
    no_id = position.get("noToken") or position.get("no_token") or position.get("noTokenId")
    if side == "YES" and yes_id:
        return str(yes_id)
    if side == "NO" and no_id:
        return str(no_id)
    return None


def _extract_position_amount(position: Dict[str, Any]) -> Optional[float]:
    if not isinstance(position, dict):
        return None
    keys = (
        "position_size",
        "position",
        "size",
        "net_position",
        "netPosition",
        "quantity",
        "amount",
        "balance",
        "shares",
        "tokens",
        "available",
        "tokenBalance",
        "holdings",
    )
    for key in keys:
        val = position.get(key)
        if val is None:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if abs(num) <= 1e-9:
            return 0.0
        return abs(num)
    return None


def _is_balance_exhausted_error(message: Optional[str]) -> bool:
    if not message:
        return False
    lowered = str(message).lower()
    keywords = [
        "not enough balance",
        "insufficient balance",
        "not enough allowance",
        "insufficient allowance",
    ]
    return any(key in lowered for key in keywords)


def _resolve_positions_address(client) -> Optional[str]:
    # Data-API 通过公开地址查询仓位，优先使用客户端暴露的钱包/代理地址。
    wallet = _extract_wallet_address(client)
    if wallet:
        return wallet
    env_addr = os.getenv("POLY_POSITIONS_ADDRESS")
    if env_addr:
        normalized = _normalize_wallet_address(env_addr)
        if normalized:
            return normalized
    return None


def _fetch_positions_from_data_api(
    client,
    *,
    redeemable: Optional[bool] = None,
) -> Tuple[List[Dict[str, Any]], bool, str]:
    address = _resolve_positions_address(client)
    if not address:
        return [], False, "未能确定仓位查询地址（请确认 funder/代理钱包）"

    limit = 500
    offset = 0
    aggregated: List[Dict[str, Any]] = []
    params: Dict[str, Any] = {
        "user": address,
        "sizeThreshold": 0,
        "limit": limit,
    }
    if redeemable is not None:
        params["redeemable"] = "true" if redeemable else "false"

    while True:
        params["offset"] = offset
        try:
            resp = requests.get(
                f"{DATA_API_ROOT}/positions", params=params, timeout=10
            )
        except requests.RequestException as exc:
            return [], False, f"data-api 请求失败：{exc}"

        if resp.status_code == 404:
            return [], True, f"data-api positions({address})"
        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            return [], False, f"data-api 返回错误：{exc}"

        try:
            payload = resp.json()
        except ValueError:
            return [], False, "data-api 返回内容非 JSON"

        page_positions = _extract_positions_from_data_api_response(payload)
        if page_positions is None:
            detail = _describe_payload_shape(payload)
            preview = _preview_payload(payload)
            return [], False, f"data-api 未识别的返回格式：{detail} 示例={preview}"

        aggregated.extend(page_positions)

        if len(page_positions) < limit:
            break

        total = None
        if isinstance(payload, dict):
            meta = payload.get("meta")
            if isinstance(meta, dict):
                total_val = meta.get("total")
                try:
                    total = int(total_val)
                except (TypeError, ValueError):
                    total = None
        offset += limit
        if total is not None and offset >= total:
            break

    return aggregated, True, f"data-api positions({address})"


def _fetch_positions_any(client) -> Tuple[List[Dict[str, Any]], bool, str]:
    return _fetch_positions_from_data_api(client)


def _get_remote_position_size(
    client,
    token_id: str,
    *,
    tolerance: float = 1e-6,
) -> Tuple[Optional[float], bool, str]:
    positions, success, info = _fetch_positions_any(client)
    if not success:
        return None, False, info

    target = str(token_id)
    for pos in positions:
        tid = _extract_token_id_from_position(pos)
        if tid and str(tid) == target:
            amount = _extract_position_amount(pos)
            if amount is None:
                return None, False, f"无法解析仓位数量（{info}）"
            if amount <= tolerance:
                return 0.0, True, info
            return float(amount), True, info
    return 0.0, True, info
def _extract_api_creds(client) -> Optional[Dict[str, str]]:
    def _pair_from_mapping(mp: Dict[str, Any]) -> Optional[Dict[str, str]]:
        if not isinstance(mp, dict):
            return None
        key_keys = ("key", "apiKey", "api_key", "id", "apiId", "api_id")
        secret_keys = ("secret", "apiSecret", "api_secret", "apiSecretKey")
        key_val = next((mp.get(k) for k in key_keys if mp.get(k)), None)
        secret_val = next((mp.get(k) for k in secret_keys if mp.get(k)), None)
        if key_val and secret_val:
            return {"key": str(key_val), "secret": str(secret_val)}
        return None

    def _pair_from_object(obj: Any) -> Optional[Dict[str, str]]:
        if obj is None:
            return None
        # 对部分库返回的命名元组/数据类做兼容
        for attr_key in ("key", "apiKey", "api_key", "id", "apiId", "api_id"):
            key_val = getattr(obj, attr_key, None)
            if key_val:
                break
        else:
            key_val = None
        for attr_secret in ("secret", "apiSecret", "api_secret", "apiSecretKey"):
            secret_val = getattr(obj, attr_secret, None)
            if secret_val:
                break
        else:
            secret_val = None
        if key_val and secret_val:
            return {"key": str(key_val), "secret": str(secret_val)}
        if hasattr(obj, "to_dict"):
            try:
                return _pair_from_mapping(obj.to_dict())
            except Exception:
                return None
        return None

    def _pair_from_sequence(seq: Any) -> Optional[Dict[str, str]]:
        if not isinstance(seq, (list, tuple)) or len(seq) < 2:
            return None
        key_val, secret_val = seq[0], seq[1]
        if key_val and secret_val:
            return {"key": str(key_val), "secret": str(secret_val)}
        return None

    candidates = [
        getattr(client, "api_creds", None),
        getattr(client, "_api_creds", None),
    ]
    getter = getattr(client, "get_api_creds", None)
    if callable(getter):
        try:
            candidates.append(getter())
        except Exception:
            pass
    key = getattr(client, "api_key", None)
    secret = getattr(client, "api_secret", None)
    if key and secret:
        candidates.append({"key": key, "secret": secret})
    # 兼容直接从环境变量注入 API key/secret 的场景
    env_key = os.getenv("POLY_API_KEY")
    env_secret = os.getenv("POLY_API_SECRET")
    if env_key and env_secret:
        candidates.append({"key": env_key, "secret": env_secret})

    for cand in candidates:
        if cand is None:
            continue
        pair = None
        if isinstance(cand, dict):
            pair = _pair_from_mapping(cand)
        elif isinstance(cand, (list, tuple)):
            pair = _pair_from_sequence(cand)
        else:
            pair = _pair_from_object(cand)
        if pair:
            return pair
    return None


def _normalize_wallet_address(val: Any) -> Optional[str]:
    if not val:
        return None
    if isinstance(val, str):
        text = val.strip()
        return text or None
    if isinstance(val, (bytes, bytearray)):
        try:
            text = val.decode("utf-8").strip()
        except Exception:
            return None
        return text or None
    if isinstance(val, dict):
        for key in (
            "address",
            "wallet",
            "wallet_address",
            "walletAddress",
            "account",
            "owner",
            "public",
        ):
            nested = val.get(key)
            normalized = _normalize_wallet_address(nested)
            if normalized:
                return normalized
        return None
    try:
        text = str(val).strip()
    except Exception:
        return None
    return text or None


def _extract_wallet_address(client) -> Optional[str]:
    attr_names = (
        "wallet_address",
        "walletAddress",
        "address",
        "owner_address",
        "ownerAddress",
        "public_address",
        "publicAddress",
        "account_address",
        "accountAddress",
        "account",
        "wallet",
        "default_address",
        "defaultAddress",
        "funder",
    )
    for name in attr_names:
        try:
            raw = getattr(client, name)
        except Exception:
            continue
        normalized = _normalize_wallet_address(raw)
        if normalized:
            return normalized

    getter_names = (
        "get_wallet_address",
        "get_wallet",
        "get_address",
        "get_owner",
        "get_account",
        "get_funder",
        "default_account",
        "default_wallet",
    )
    for name in getter_names:
        fn = getattr(client, name, None)
        if not callable(fn):
            continue
        try:
            raw = fn()
        except TypeError:
            continue
        except Exception:
            continue
        normalized = _normalize_wallet_address(raw)
        if normalized:
            return normalized
    return None


def _http_json(url: str, params=None) -> Optional[Any]:
    try:
        r = requests.get(url, params=params or {}, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def _list_markets_under_event(event_slug: str) -> List[dict]:
    if not event_slug:
        return []
    # A) /events?slug=<slug>
    data = _http_json(f"{GAMMA_ROOT}/events", params={"slug": event_slug, "closed": "false"})
    evs = []
    if isinstance(data, dict) and "data" in data:
        evs = data["data"]
    elif isinstance(data, list):
        evs = data
    if isinstance(evs, list):
        for ev in evs:
            mkts = ev.get("markets") or []
            if mkts:
                return mkts
    # B) /markets?search=<slug> 精确过滤 eventSlug
    data = _http_json(f"{GAMMA_ROOT}/markets", params={"limit": 200, "active": "true", "search": event_slug})
    mkts = []
    if isinstance(data, dict) and "data" in data:
        mkts = data["data"]
    elif isinstance(data, list):
        mkts = data
    if isinstance(mkts, list):
        return [m for m in mkts if str(m.get("eventSlug") or "") == str(event_slug)]
    return []

def _fetch_market_by_slug(market_slug: str) -> Optional[dict]:
    return _http_json(f"{GAMMA_ROOT}/markets/slug/{market_slug}")

def _pick_market_subquestion(markets: List[dict]) -> dict:
    print("[CHOICE] 该事件下存在多个子问题，请选择其一，或直接粘贴具体子问题URL：")
    for i, m in enumerate(markets):
        title = m.get("title") or m.get("question") or m.get("slug")
        end_ts = m.get("endDate") or m.get("endTime") or ""
        mslug = m.get("slug") or ""
        url = f"https://polymarket.com/market/{mslug}" if mslug else "(no slug)"
        print(f"  [{i}] {title}  (end={end_ts})  -> {url}")
    while True:
        s = input("请输入序号或粘贴URL：").strip()
        if s.startswith(("http://", "https://")):
            return {"__direct_url__": s}
        if s.isdigit():
            idx = int(s)
            if 0 <= idx < len(markets):
                return markets[idx]
        print("请输入有效序号或URL。")

def _tokens_from_market_obj(m: dict) -> Tuple[str, str, str]:
    title = m.get("title") or m.get("question") or m.get("slug") or ""
    yes_id = no_id = ""
    ids = m.get("clobTokenIds") or m.get("clobTokens")
    if isinstance(ids, (list, tuple)) and len(ids) >= 2:
        return str(ids[0]), str(ids[1]), title
    outcomes = m.get("outcomes") or []
    if outcomes and isinstance(outcomes[0], dict):
        for o in outcomes:
            name = (o.get("name") or o.get("outcome") or "").strip().lower()
            tid = o.get("tokenId") or o.get("clobTokenId") or ""
            if not tid: continue
            if name in ("yes", "y", "true"): yes_id = str(tid)
            elif name in ("no", "n", "false"): no_id = str(tid)
        if yes_id and no_id:
            return yes_id, no_id, title
    return yes_id, no_id, title

def _resolve_with_fallback(source: str) -> Tuple[str, str, str, Dict[str, Any]]:
    # 1) "YES_id,NO_id"
    y, n = _parse_yes_no_ids_literal(source)
    if y and n:
        return y, n, "(Manual IDs)", {}
    # 2) 先尝试旧解析器（单一市场 URL/slug）
    try:
        y1, n1, title1, raw1 = resolve_token_ids(source)
        if y1 and n1:
            meta = _market_meta_from_obj(raw1 or {}) if raw1 else {}
            if not meta:
                meta = _maybe_fetch_market_meta_from_source(source)
            return y1, n1, title1, meta
    except Exception:
        pass
    # 3) 事件页/事件 slug 回退链路
    event_slug = _extract_event_slug(source)
    if not event_slug:
        raise ValueError("无法从输入中提取事件 slug，且直接解析失败。")
    mkts = _list_markets_under_event(event_slug)
    if not mkts:
        raise ValueError(f"未在事件 {event_slug} 下检索到子问题列表。")
    chosen = _pick_market_subquestion(mkts)
    if "__direct_url__" in chosen:
        y2, n2, title2, raw2 = resolve_token_ids(chosen["__direct_url__"])
        if y2 and n2:
            meta = _market_meta_from_obj(raw2 or {}) if raw2 else {}
            if not meta:
                meta = _maybe_fetch_market_meta_from_source(chosen["__direct_url__"])
            return y2, n2, title2, meta
        raise ValueError("无法从粘贴的URL解析出 tokenId。")
    y3, n3, title3 = _tokens_from_market_obj(chosen)
    if y3 and n3:
        meta = _market_meta_from_obj(chosen)
        return y3, n3, title3, meta
    slug2 = chosen.get("slug") or ""
    if slug2:
        # 兜底：拉完整市场详情；若还不行，再把 /market/<slug> 丢给旧解析器
        m_full = _fetch_market_by_slug(slug2)
        if m_full:
            y4, n4, title4 = _tokens_from_market_obj(m_full)
            if y4 and n4:
                meta = _market_meta_from_obj(m_full)
                return y4, n4, title4, meta
        y5, n5, title5, raw5 = resolve_token_ids(f"https://polymarket.com/market/{slug2}")
        if y5 and n5:
            meta = _market_meta_from_obj(raw5 or {}) if raw5 else {}
            if not meta:
                meta = _maybe_fetch_market_meta_from_source(f"https://polymarket.com/market/{slug2}")
            return y5, n5, title5, meta
    raise ValueError("子问题未包含 tokenId，且兜底解析失败。")

# ====== 下单执行工具 ======
def _place_buy(
    client,
    token_id: str,
    price: float,
    size: float,
    *,
    min_order_size: float = 0.0,
    tick_size: float = 0.0,
) -> ExecutionResult:
    return execute_auto_buy(
        client=client,
        token_id=token_id,
        price=price,
        size=size,
        min_order_size=min_order_size,
        tick_size=tick_size,
    )


def _place_sell(client, token_id: str, price: float, size: float) -> ExecutionResult:
    return execute_auto_sell(client=client, token_id=token_id, price=price, size=size)

# ===== 主流程 =====
def main():
    client = _get_client()
    creds_check = _extract_api_creds(client)
    if not creds_check or not creds_check.get("key") or not creds_check.get("secret"):
        print("[ERR] 无法获取完整 API 凭证，请检查配置后重试。")
        return
    print("[INIT] API 凭证已验证。")
    print("[INIT] ClobClient 就绪。")
    print('请输入 Polymarket 市场 URL，或 "YES_id,NO_id"：')
    source = input().strip()
    if not source:
        print("[ERR] 未输入，退出。")
        return
    try:
        yes_id, no_id, title, market_meta = _resolve_with_fallback(source)
    except Exception as e:
        print("[ERR] 无法解析目标：", e)
        return
    market_meta = market_meta or {}
    print(f"[INFO] 市场/子问题标题: {title}")
    print(f"[INFO] 解析到 tokenIds: YES={yes_id} | NO={no_id}")

    min_order_size_hint = _coerce_positive_float(market_meta.get("minimum_order_size"))
    tick_size_hint = _coerce_positive_float(market_meta.get("minimum_tick_size"))
    if min_order_size_hint:
        print(f"[INFO] 市场最小下单份数: {min_order_size_hint}")
    if tick_size_hint:
        print(f"[INFO] 市场价格最小跳动: {tick_size_hint}")

    def _fmt_ts(ts_val: Optional[float]) -> Optional[str]:
        if ts_val is None:
            return None
        try:
            ts_f = float(ts_val)
        except (TypeError, ValueError):
            return None
        dt = datetime.fromtimestamp(ts_f, tz=timezone.utc)
        return dt.isoformat()

    end_ts = market_meta.get("end_ts") if isinstance(market_meta, dict) else None
    resolved_ts = market_meta.get("resolved_ts") if isinstance(market_meta, dict) else None
    if end_ts or resolved_ts:
        end_str = _fmt_ts(end_ts)
        resolve_str = _fmt_ts(resolved_ts)
        if end_str:
            print(f"[INFO] 市场计划截止时间 (UTC): {end_str}")
        if resolve_str and resolve_str != end_str:
            print(f"[INFO] 市场预计结算时间 (UTC): {resolve_str}")

    def _calc_deadline(meta: Dict[str, Any]) -> Optional[float]:
        candidates: List[float] = []
        if isinstance(meta, dict):
            for key in ("end_ts", "resolved_ts"):
                ts_val = meta.get(key)
                if isinstance(ts_val, (int, float)):
                    candidates.append(float(ts_val))
        return min(candidates) if candidates else None

    market_deadline_ts = _calc_deadline(market_meta)
    if market_deadline_ts:
        dt_deadline = datetime.fromtimestamp(market_deadline_ts, tz=timezone.utc)
        print(
            "[INFO] 监控目标结束时间 (UTC): "
            f"{dt_deadline.isoformat()}"
        )
    else:
        print("[ERR] 未能获取市场结束时间，程序终止。")
        return

    print('请选择方向（YES/NO），回车确认：')
    side = input().strip().upper()
    if side not in ("YES", "NO"):
        print("[ERR] 方向非法，退出。")
        return
    token_id = yes_id if side == "YES" else no_id

    print("请输入倒计时提醒起始分钟数（默认 5）：")
    countdown_input = input().strip()
    try:
        countdown_minutes = float(countdown_input) if countdown_input else 5.0
    except Exception:
        print("[ERR] 倒计时起始分钟数非法，退出。")
        return
    if countdown_minutes < 0:
        print("[ERR] 倒计时起始分钟数不可为负，退出。")
        return
    countdown_display_seconds = countdown_minutes * 60.0

    print("请输入买入份数（留空=按 $1 反推）：")
    size_in = input().strip()
    print("请输入买入实时价格下限（默认 0.25，低于该实时价格不买入）：")
    min_price_input = input().strip()
    try:
        min_realtime_price = float(min_price_input) if min_price_input else 0.25
    except Exception:
        print("[ERR] 买入实时价格下限非法，退出。")
        return
    if min_realtime_price < 0:
        print("[ERR] 买入实时价格下限不可为负，退出。")
        return

    print("请输入买入触发价（对标 ask，如 0.35，留空表示仅依赖跌幅触发）：")
    buy_px_in = input().strip()
    buy_threshold = None
    if buy_px_in:
        try:
            buy_threshold = float(buy_px_in)
        except Exception:
            print("[ERR] 触发价非法，退出。")
            return

    print("请输入跌幅窗口分钟数（默认 10）：")
    drop_window_in = input().strip()
    try:
        drop_window = float(drop_window_in) if drop_window_in else 10.0
    except Exception:
        print("[ERR] 跌幅窗口非法，退出。")
        return

    print("请输入跌幅触发百分比（默认 5 表示 5%）：")
    drop_pct_in = input().strip()
    try:
        drop_pct = float(drop_pct_in) / 100.0 if drop_pct_in else 0.05
    except Exception:
        print("[ERR] 跌幅百分比非法，退出。")
        return

    print("请输入卖出盈利百分比（默认 5 表示 +5%）：")
    profit_in = input().strip()
    try:
        profit_pct = float(profit_in) / 100.0 if profit_in else 0.05
    except Exception:
        print("[ERR] 盈利百分比非法，退出。")
        return

    print("是否启用“每次卖出后将下一次买入跌幅阈值+1%（上限20%）”的动态功能？(默认启用，输入 n/N 禁用)：")
    incremental_flag = input().strip().lower()
    enable_incremental_drop_pct = incremental_flag not in {"n", "no", "0", "false"}

    cfg = StrategyConfig(
        token_id=token_id,
        buy_price_threshold=buy_threshold,
        drop_window_minutes=drop_window,
        drop_pct=drop_pct,
        profit_pct=profit_pct,
        enable_incremental_drop_pct=enable_incremental_drop_pct,
    )
    strategy = VolArbStrategy(cfg)

    latest: Dict[str, Dict[str, Any]] = {}
    action_queue: Queue[Action] = Queue()
    stop_event = threading.Event()
    countdown_phase_event = threading.Event()
    market_closed_detected = False

    slug_for_refresh = ""
    if isinstance(market_meta, dict):
        slug_for_refresh = (
            str(market_meta.get("slug") or "")
            or str(market_meta.get("market_slug") or "")
        )
        if not slug_for_refresh:
            raw_meta = market_meta.get("raw") if isinstance(market_meta, dict) else {}
            if isinstance(raw_meta, dict):
                slug_for_refresh = str(raw_meta.get("slug") or "")
    if not slug_for_refresh:
        slug_for_refresh = _extract_market_slug(source)
    unable_to_refresh_logged = False

    def _refresh_market_meta() -> Dict[str, Any]:
        nonlocal market_meta, market_deadline_ts, unable_to_refresh_logged
        slug = slug_for_refresh
        if not slug:
            if not unable_to_refresh_logged:
                print("[COUNTDOWN] 无市场 slug，无法刷新事件状态，仅依赖本地信息。")
                unable_to_refresh_logged = True
            return market_meta
        m_obj = _fetch_market_by_slug(slug)
        if isinstance(m_obj, dict):
            refreshed = _market_meta_from_obj(m_obj)
            if refreshed:
                if (
                    market_meta
                    and "minimum_order_size" in market_meta
                    and "minimum_order_size" not in refreshed
                ):
                    refreshed["minimum_order_size"] = market_meta["minimum_order_size"]
                if (
                    market_meta
                    and "minimum_tick_size" in market_meta
                    and "minimum_tick_size" not in refreshed
                ):
                    refreshed["minimum_tick_size"] = market_meta["minimum_tick_size"]
                market_meta = refreshed
                new_deadline = _calc_deadline(market_meta)
                if new_deadline:
                    market_deadline_ts = new_deadline
        return market_meta

    def _calc_size_by_1dollar(ask_px: float) -> float:
        if not ask_px or ask_px <= 0:
            return 1.0
        s = 1.0 / ask_px
        return float(Decimal(str(s)).quantize(Decimal("1"), rounding=ROUND_UP))

    def _extract_ts(raw: Optional[Any]) -> float:
        if raw is None:
            return time.time()
        try:
            ts = float(raw)
        except Exception:
            return time.time()
        if ts > 1e12:
            ts = ts / 1000.0
        return ts

    def _is_market_closed(payload: Dict[str, Any]) -> bool:
        status_keys = ["status", "market_status", "marketStatus"]
        for key in status_keys:
            val = payload.get(key)
            if isinstance(val, str) and val.lower() in {"closed", "settled", "resolved", "expired"}:
                return True
        bool_keys = ["is_closed", "market_closed", "closed", "isMarketClosed"]
        for key in bool_keys:
            val = payload.get(key)
            if isinstance(val, bool) and val:
                return True
            if isinstance(val, str) and val.strip().lower() in {"true", "1", "yes"}:
                return True
        return False

    def _event_indicates_market_closed(ev: Dict[str, Any]) -> bool:
        if not isinstance(ev, dict):
            return False

        if _is_market_closed(ev):
            return True

        queue: List[Dict[str, Any]] = []
        for key in ("market", "market_state", "marketState", "marketStatus", "data", "payload"):
            val = ev.get(key)
            if isinstance(val, dict):
                queue.append(val)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        queue.append(item)

        while queue:
            item = queue.pop()
            if _is_market_closed(item):
                return True
            for key, val in item.items():
                if isinstance(val, dict):
                    queue.append(val)
                elif isinstance(val, list):
                    for sub in val:
                        if isinstance(sub, dict):
                            queue.append(sub)
        return False

    def _extract_price(resp: Any, fallback: float) -> float:
        if isinstance(resp, ExecutionResult):
            if resp.avg_price is not None:
                return float(resp.avg_price)
            if resp.last_price is not None:
                return float(resp.last_price)
            if resp.limit_price is not None:
                return float(resp.limit_price)
            return float(fallback)
        if isinstance(resp, dict):
            for key in ("avg_price", "avgPrice", "filled_avg_price", "filledAvgPrice", "price"):
                val = resp.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except Exception:
                        pass
        return float(fallback)

    def _extract_size(resp: Any, fallback: float) -> float:
        if isinstance(resp, ExecutionResult):
            return float(resp.filled or fallback)
        if isinstance(resp, dict):
            for key in ("filled", "filled_size", "filledSize", "size"):
                val = resp.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except Exception:
                        pass
        return float(fallback)

    def _extract_remaining(resp: Any, requested: float) -> Optional[float]:
        if isinstance(resp, ExecutionResult):
            return float(resp.remaining)
        if isinstance(resp, dict):
            for key in (
                "remaining",
                "remaining_size",
                "remainingSize",
                "open_size",
                "openSize",
                "unfilled",
                "unfilledSize",
            ):
                val = resp.get(key)
                if val is not None:
                    try:
                        return max(float(val), 0.0)
                    except Exception:
                        pass

            filled_val: Optional[float] = None
            for key in ("filled", "filled_size", "filledSize", "size"):
                val = resp.get(key)
                if val is not None:
                    try:
                        filled_val = float(val)
                        break
                    except Exception:
                        continue
            if filled_val is None:
                return None

            try:
                requested_f = float(requested)
            except (TypeError, ValueError):
                return None

            remaining = requested_f - filled_val
            if remaining < 0 and abs(remaining) <= 1e-6:
                remaining = 0.0
            return max(remaining, 0.0)

        return None

    def _status_lower(resp: Any) -> str:
        if isinstance(resp, ExecutionResult):
            return resp.status.lower()
        if isinstance(resp, dict):
            val = resp.get("status")
            if isinstance(val, str):
                return val.lower()
        if isinstance(resp, str):
            return resp.lower()
        return ""

    def _parse_price_change(pc: Dict[str, Any]) -> Tuple[float, float, float]:
        def _to_float(val: Any) -> Optional[float]:
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        price_fields = (
            "last_trade_price",
            "last_price",
            "mark_price",
            "price",
        )

        bid = _to_float(pc.get("best_bid"))
        ask = _to_float(pc.get("best_ask"))

        price_val: Optional[float] = None
        for key in price_fields:
            price_val = _to_float(pc.get(key))
            if price_val is not None:
                break

        if price_val is None:
            if bid is not None and ask is not None:
                price_val = (bid + ask) / 2.0
            elif bid is not None:
                price_val = bid
            elif ask is not None:
                price_val = ask
            else:
                price_val = 0.0

        return (
            bid or 0.0,
            ask or 0.0,
            price_val,
        )

    def _on_event(ev: Dict[str, Any]):
        nonlocal market_closed_detected
        if stop_event.is_set():
            return
        if not isinstance(ev, dict):
            return
        if _event_indicates_market_closed(ev):
            print("[MARKET] 收到市场关闭事件，准备退出…")
            market_closed_detected = True
            strategy.stop("market closed")
            stop_event.set()
            return

        if ev.get("event_type") == "price_change":
            pcs = ev.get("price_changes", [])
        elif "price_changes" in ev:
            pcs = ev.get("price_changes", [])
        else:
            return
        ts = _extract_ts(ev.get("timestamp") or ev.get("ts") or ev.get("time"))
        for pc in pcs:
            if str(pc.get("asset_id")) != str(token_id):
                continue
            bid, ask, last = _parse_price_change(pc)
            latest[token_id] = {"price": last, "best_bid": bid, "best_ask": ask}
            action = strategy.on_tick(best_ask=ask, best_bid=bid, ts=ts)
            if action:
                action_queue.put(action)
            if _is_market_closed(pc):
                print("[MARKET] 检测到市场关闭信号，准备退出…")
                market_closed_detected = True
                strategy.stop("market closed")
                stop_event.set()
                break

    def _confirm_market_closed():
        nonlocal market_closed_detected
        attempt = 0
        while not stop_event.is_set():
            refreshed_meta = _refresh_market_meta()
            attempt += 1
            now = time.time()
            if _market_has_ended(refreshed_meta, now):
                print("[MARKET] 已确认市场结束，可进行后续处理。")
                market_closed_detected = True
                strategy.stop("market ended confirmed")
                stop_event.set()
                return
            if attempt == 1:
                print("[MARKET] 倒计时结束但市场尚未标记结束，10 秒后再次检查…")
            else:
                print(
                    f"[MARKET] 第 {attempt} 次检查仍未确认结束，10 秒后再次重试…"
                )
            for _ in range(10):
                if stop_event.is_set():
                    return
                time.sleep(1)

    def _countdown_monitor():
        if not market_deadline_ts:
            return
        last_display: Optional[int] = None
        while not stop_event.is_set():
            now = time.time()
            remaining = market_deadline_ts - now
            if remaining <= 0:
                if not countdown_phase_event.is_set():
                    countdown_phase_event.set()
                if last_display != 0:
                    print("[COUNTDOWN] 距离市场结束还剩 00:00")
                print("[COUNTDOWN] 倒计时结束，开始确认市场状态…")
                _confirm_market_closed()
                return
            if remaining <= countdown_display_seconds:
                if not countdown_phase_event.is_set():
                    countdown_phase_event.set()
                    print(
                        f"[COUNTDOWN] 已进入倒计时提醒阶段（≤ {countdown_minutes:.2f} 分钟）。"
                    )
                secs_left = int(remaining)
                if secs_left != last_display:
                    mm = secs_left // 60
                    ss = secs_left % 60
                    print(
                        f"[COUNTDOWN] 距离市场结束还剩 {mm:02d}:{ss:02d}"
                    )
                    last_display = secs_left
                for _ in range(5):
                    if stop_event.is_set():
                        return
                    time.sleep(0.2)
            else:
                wait = min(remaining - countdown_display_seconds, 60)
                if wait <= 0:
                    wait = 1
                sleep_steps = max(int(wait), 1)
                for _ in range(sleep_steps):
                    if stop_event.is_set():
                        return
                    time.sleep(1)

    ws_thread = threading.Thread(
        target=ws_watch_by_ids,
        kwargs={
            "asset_ids": [token_id],
            "label": f"{title} ({side})",
            "on_event": _on_event,
            "verbose": False,
            "stop_event": stop_event,
        },
        daemon=True,
    )
    ws_thread.start()

    print("[RUN] 监听行情中… 输入 stop / exit 可手动停止。")

    if market_deadline_ts:
        threading.Thread(target=_countdown_monitor, daemon=True).start()

    start_wait = time.time()
    while not latest.get(token_id) and not stop_event.is_set():
        if time.time() - start_wait > 5:
            print("[WAIT] 尚未收到行情，继续等待…")
            start_wait = time.time()
        time.sleep(0.2)

    if stop_event.is_set():
        print("[EXIT] 已终止。")
        return

    def _input_listener():
        while not stop_event.is_set():
            try:
                cmd = input().strip().lower()
            except EOFError:
                break
            if cmd in {"stop", "exit", "quit"}:
                print("[CMD] 收到停止指令，准备退出…")
                strategy.stop("manual stop")
                stop_event.set()
                break

    threading.Thread(target=_input_listener, daemon=True).start()

    success_status = {
        "success",
        "matched",
        "filled",
        "complete",
        "completed",
    }
    position_size: Optional[float] = None
    last_order_size: Optional[float] = None
    pending_sell_fill_price: Optional[float] = None
    last_log = 0.0
    buy_cooldown_until: float = 0.0
    last_buy_cooldown_log: float = 0.0

    def _complete_sell(avg_price: float, detail: str = ""):
        nonlocal position_size, last_order_size, pending_sell_fill_price
        nonlocal buy_cooldown_until, last_buy_cooldown_log
        strategy.on_sell_filled(avg_price=avg_price)
        position_size = None
        last_order_size = None
        pending_sell_fill_price = None
        buy_cooldown_until = time.time() + 15.0
        last_buy_cooldown_log = 0.0
        msg = f"[STATE] 卖出成交 -> price={avg_price:.4f}"
        if detail:
            msg = f"{msg} {detail}"
        print(msg)
        if countdown_phase_event.is_set():
            print(
                "[COUNTDOWN] 倒计时阶段内卖出成交，程序将提前结束以避免重新买入。"
            )
            strategy.stop("countdown sell exit")
            stop_event.set()

    try:
        while not stop_event.is_set():
            now = time.time()

            if now - last_log >= 1.0:
                snap = latest.get(token_id) or {}
                bid = float(snap.get("best_bid") or 0.0)
                ask = float(snap.get("best_ask") or 0.0)
                last_px = float(snap.get("price") or 0.0)
                st = strategy.status()
                awaiting = st.get("awaiting")
                awaiting_s = awaiting.value if hasattr(awaiting, "value") else awaiting
                entry_price = st.get("entry_price")
                print(
                    f"[PX] bid={bid:.4f} ask={ask:.4f} last={last_px:.4f} | "
                    f"state={st.get('state')} awaiting={awaiting_s} entry={entry_price}"
                )

                extra_lines = []
                if st.get("state") == "LONG":
                    sell_target = st.get("sell_trigger")
                    if sell_target is not None:
                        extra_lines.append(f"    目标卖出价格: {float(sell_target):.4f}")
                    else:
                        extra_lines.append("    目标卖出价格: -")

                drop_stats = st.get("drop_stats") or {}
                current_drop_ratio = drop_stats.get("current_drop_ratio")
                if isinstance(current_drop_ratio, (int, float)):
                    details = []
                    window_high = drop_stats.get("window_high")
                    if isinstance(window_high, (int, float)):
                        details.append(f"高点{float(window_high):.4f}")
                    window_low = drop_stats.get("window_low")
                    if isinstance(window_low, (int, float)):
                        details.append(f"低点{float(window_low):.4f}")
                    line = f"    当前实时跌幅: {current_drop_ratio * 100:.2f}%"
                    if details:
                        line += " (" + " / ".join(details) + ")"
                    extra_lines.append(line)

                for line in extra_lines:
                    print(line)
                last_log = now

            try:
                action = action_queue.get(timeout=0.5)
            except Empty:
                continue

            if stop_event.is_set():
                break

            snap = latest.get(token_id) or {}
            bid = float(snap.get("best_bid") or 0.0)
            ask = float(snap.get("best_ask") or 0.0)

            if (
                not market_closed_detected
                and market_meta
                and _market_has_ended(market_meta, now)
            ):
                print("[MARKET] 达到市场截止时间，准备退出…")
                market_closed_detected = True
                strategy.stop("market ended")
                stop_event.set()
                continue

            if action.action == ActionType.BUY:
                now_for_buy = time.time()
                if now_for_buy < buy_cooldown_until:
                    remaining = buy_cooldown_until - now_for_buy
                    if now_for_buy - last_buy_cooldown_log >= 5.0:
                        print(
                            f"[COOLDOWN] 冷却期内暂停买入，剩余 {max(0.0, remaining):.1f}s 后再尝试。"
                        )
                        last_buy_cooldown_log = now_for_buy
                    strategy.on_reject("buy cooldown active")
                    continue

                realtime_price = ask if ask > 0 else float(snap.get("price") or 0.0)
                if realtime_price < min_realtime_price:
                    print(
                        "[SKIP] 实时价格 "
                        f"{realtime_price:.4f} 低于买入下限 {min_realtime_price:.4f}，跳过本次买入信号。"
                    )
                    strategy.on_reject("realtime price below min threshold")
                    continue

                ref_price = action.ref_price or ask or float(snap.get("price") or 0.0)
                if size_in:
                    try:
                        order_size = float(size_in)
                    except Exception:
                        print("[ERR] 份数非法，终止。")
                        strategy.stop("invalid size")
                        stop_event.set()
                        break
                else:
                    order_size = _calc_size_by_1dollar(ref_price)
                    print(f"[HINT] 未指定份数，按 $1 反推 -> size={order_size}")

                market_min_order = _coerce_positive_float(
                    (market_meta or {}).get("minimum_order_size")
                )
                market_tick_size = _coerce_positive_float(
                    (market_meta or {}).get("minimum_tick_size")
                )

                try:
                    resp = _place_buy(
                        client=client,
                        token_id=token_id,
                        price=ref_price,
                        size=order_size,
                        min_order_size=market_min_order or 0.0,
                        tick_size=market_tick_size or 0.0,
                    )
                except Exception as exc:
                    print(f"[ERR] 买入下单异常：{exc}")
                    strategy.on_reject(str(exc))
                    continue
                print(f"[TRADE][BUY] resp={resp}")
                status = _status_lower(resp)
                fill_px = _extract_price(resp, ref_price)
                fill_size = _extract_size(resp, order_size)
                filled_positive = fill_size is not None and float(fill_size) > 0
                if status not in success_status and filled_positive:
                    status = "filled"
                if status in success_status:
                    try:
                        position_size = float(fill_size)
                        last_order_size = float(fill_size)
                    except Exception:
                        position_size = float(order_size)
                        last_order_size = float(order_size)
                    pending_sell_fill_price = None
                    strategy.on_buy_filled(avg_price=fill_px, size=position_size)
                    print(
                        f"[STATE] 买入成交 -> price={fill_px:.4f} size={position_size:.4f}"
                    )
                else:
                    reason = (
                        resp.message
                        if isinstance(resp, ExecutionResult) and resp.message
                        else (resp.get("message") if isinstance(resp, dict) else str(resp))
                    )
                    print(f"[WARN] 买入未成交：{reason}")
                    strategy.on_reject(reason if isinstance(reason, str) else None)

            elif action.action == ActionType.SELL:
                ref_price = action.ref_price or bid or float(snap.get("price") or 0.0)
                sell_size = position_size
                if sell_size is None:
                    try:
                        st_now = strategy.status()
                        ps = st_now.get("position_size")
                        sell_size = float(ps) if ps is not None else None
                    except Exception:
                        sell_size = None
                if sell_size is None or sell_size <= 0:
                    sell_size = last_order_size
                if sell_size is None or sell_size <= 0:
                    print("[WARN] 无可用持仓，忽略卖出信号。")
                    strategy.on_reject("empty position")
                    continue

                try:
                    resp = _place_sell(
                        client=client,
                        token_id=token_id,
                        price=ref_price,
                        size=sell_size,
                    )
                except Exception as exc:
                    err_text = str(exc)
                    print(f"[ERR] 卖出下单异常：{err_text}")
                    if _is_balance_exhausted_error(err_text):
                        assumed_price = pending_sell_fill_price
                        if assumed_price is None:
                            try:
                                assumed_price = float(ref_price)
                            except Exception:
                                assumed_price = 0.0
                        print(
                            "[INFO] 卖出被拒绝且提示余额不足，视作仓位已清空，跳过后续卖出重试。"
                        )
                        _complete_sell(float(assumed_price or 0.0), "(余额不足提示)")
                        continue
                    strategy.on_reject(err_text)
                    continue
                print(f"[TRADE][SELL] resp={resp}")
                status = _status_lower(resp)
                fill_px = _extract_price(resp, ref_price)
                filled_size = _extract_size(resp, 0.0)
                try:
                    sell_size_f = float(sell_size)
                except (TypeError, ValueError):
                    sell_size_f = 0.0
                remaining_size = _extract_remaining(resp, sell_size_f)

                fully_filled = False
                if isinstance(resp, ExecutionResult):
                    fully_filled = resp.remaining <= 1e-9
                elif remaining_size is not None:
                    fully_filled = remaining_size <= 1e-9 and status in success_status
                elif status in success_status and sell_size_f > 0 and filled_size >= sell_size_f - 1e-6:
                    fully_filled = True

                if fully_filled:
                    try:
                        close_price = float(
                            fill_px if fill_px is not None else float(ref_price)
                        )
                    except Exception:
                        close_price = float(ref_price or 0.0)
                    pending_sell_fill_price = close_price
                    remote_size, remote_ok, remote_info = _get_remote_position_size(
                        client, token_id
                    )
                    if not remote_ok:
                        print(
                            f"[WARN] 卖出后仓位核实失败：{remote_info}，将按成交结果假定已清仓。"
                        )
                        _complete_sell(close_price, "(仓位核实失败，按成交结果处理)")
                        continue
                    if remote_size is not None and remote_size > 1e-6:
                        print(
                            f"[WARN] 卖出成交但账户仍有剩余 {remote_size:.4f}，继续等待清仓。"
                        )
                        position_size = remote_size
                        last_order_size = position_size
                        pending_sell_fill_price = close_price
                        strategy.on_reject(
                            f"position remaining {remote_size:.4f} after sell ({remote_info})"
                        )
                        continue

                    _complete_sell(close_price)
                    continue
                elif filled_size > 1e-9:
                    try:
                        close_price = float(
                            fill_px if fill_px is not None else float(ref_price)
                        )
                    except Exception:
                        close_price = float(ref_price or 0.0)
                    remaining_effective = remaining_size
                    if remaining_effective is None:
                        remaining_effective = max(sell_size_f - float(filled_size), 0.0)
                    try:
                        remaining_effective = float(remaining_effective)
                    except Exception:
                        remaining_effective = 0.0
                    if remaining_effective <= 1e-6:
                        remaining_effective = 0.0

                    remote_size, remote_ok, remote_info = _get_remote_position_size(
                        client, token_id
                    )
                    if remote_ok and remote_size is not None:
                        if remote_size <= 1e-6:
                            _complete_sell(close_price, "(partial 状态但账户已清仓)")
                            continue
                        if abs(remote_size - remaining_effective) > 1e-6:
                            print(
                                f"[INFO] 本地剩余 {remaining_effective:.4f} 与账户仓位 "
                                f"{remote_size:.4f} 不一致，改以账户仓位为准。"
                            )
                            remaining_effective = remote_size
                        pending_sell_fill_price = close_price
                    elif not remote_ok:
                        print(
                            f"[WARN] 部分成交后仓位核实失败：{remote_info}，保留当前估算。"
                        )
                        pending_sell_fill_price = close_price

                    if remaining_effective <= 0:
                        _complete_sell(close_price, "(partial 状态但剩余≈0)")
                        continue

                    position_size = remaining_effective if remaining_effective > 0 else None
                    last_order_size = position_size
                    pending_sell_fill_price = close_price

                    reason = (
                        resp.message
                        if isinstance(resp, ExecutionResult) and resp.message
                        else (resp.get("message") if isinstance(resp, dict) else str(resp))
                    )
                    detail = (
                        f"partial fill, remaining {remaining_effective:.4f}"
                        if remaining_effective > 0
                        else "partial fill"
                    )
                    if reason and reason != detail:
                        detail = f"{detail} ({reason})"

                    print(
                        f"[WARN] 卖出部分成交 -> filled={float(filled_size):.4f} "
                        f"remaining={remaining_effective:.4f}"
                    )
                    strategy.on_reject(detail)
                else:
                    reason = (
                        resp.message
                        if isinstance(resp, ExecutionResult) and resp.message
                        else (resp.get("message") if isinstance(resp, dict) else str(resp))
                    )
                    print(f"[WARN] 卖出未成交：{reason}")
                    strategy.on_reject(reason if isinstance(reason, str) else None)

    except KeyboardInterrupt:
        print("[CMD] 捕获到 Ctrl+C，准备退出…")
        strategy.stop("keyboard interrupt")
        stop_event.set()

    finally:
        stop_event.set()
        final_status = strategy.status()
        print(f"[EXIT] 最终状态: {final_status}")
        print("[INFO] 自动 claim 功能已移除，如有需要请手动在官网完成结算。")


if __name__ == "__main__":
    main()
