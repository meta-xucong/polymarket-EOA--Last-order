# Volatility_fliter_EOA.py
# -*- coding: utf-8 -*-
"""Volatility 策略市场筛选工具（EOA版）。

本脚本在 Safe 版 `Volatility_fliter.py` 的基础上迁移至 EOA 交易栈：
- 依赖改为 `Volatility_arbitrage_main_rest_EOA` / `Volatility_arbitrage_price_watch_EOA`
  等模块，确保与 EOA 私钥登录流程兼容；
- 调整部分默认行为，使其贴合 EOA 运行模式（如忽略 Safe 专属 funder 等配置）。

功能简介：
    * 从 gamma-api 或本地 JSON 文件获取市场列表；
    * 支持对活跃度、成交量、流动性、点差、剩余时间等进行过滤；
    * 输出精简表格或 JSON，帮助人工挑选适合的波动率套利市场。

注意：该脚本只做静态筛选，不会下单。网络请求可能受限，若 gamma-api
返回 403，可将 markets 数据导出为 JSON 文件后通过 `--from-file` 选项加载。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from collections.abc import Iterable as IterableABC, Mapping as MappingABC
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib import error, parse, request

from Volatility_arbitrage_main_rest_EOA import get_client as get_eoa_client
from Volatility_arbitrage_main_rest_EOA import get_api_creds_tuple
from Volatility_arbitrage_price_watch_EOA import resolve_token_ids

DEFAULT_BANNED_KEYWORDS: Tuple[str, ...] = (
    "Bitcoin",
    "BTC",
    "ETH",
    "Ethereum",
    "Sol",
    "Solana",
    "Doge",
    "Dogecoin",
    "BNB",
    "Binance",
    "Cardano",
    "ADA",
    "XRP",
    "Ripple",
    "Matic",
    "Polygon",
    "Crypto",
    "Cryptocurrency",
    "Blockchain",
    "Token",
    "NFT",
    "DeFi",
    "vs",
    "odds",
    "score",
    "spread",
    "moneyline",
    "Esports",
    "CS2",
    "Cup",
    "Arsenal",
    "Liverpool",
    "Chelsea",
    "EPL",
    "PGA",
    "Tour Championship",
    "Scottie Scheffler",
    "Vitality",
    "MOUZ",
    "Falcons",
    "The MongolZ",
    "AL",
    "Houston",
    "Chicago",
    "New York",
)

__all__ = [
    "MarketFilterConfig",
    "FilterDiagnostics",
    "OutcomeSnapshot",
    "MarketSnapshot",
    "fetch_markets",
    "filter_markets",
    "summarize_market",
    "main",
]

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
# 一些企业代理会拒绝未知的 UA，这里模仿主流浏览器字符串降低被拦截概率
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

_ORDERBOOK_CACHE: Dict[str, Tuple[Optional[float], Optional[float]]] = {}


_API_KEY_FIELDS = ("key", "apiKey", "api_key", "id", "apiId", "api_id")
_API_SECRET_FIELDS = ("secret", "apiSecret", "api_secret", "apiSecretKey")
_API_PASS_FIELDS = (
    "passphrase",
    "apiPassphrase",
    "api_passphrase",
    "apiPass",
    "pass",
)


def _normalize_str(value: Any) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value)


def _creds_from_mapping(mp: Any) -> Optional[Tuple[str, str, Optional[str]]]:
    if not isinstance(mp, dict):
        return None
    key_val = next((mp.get(name) for name in _API_KEY_FIELDS if mp.get(name)), None)
    secret_val = next((mp.get(name) for name in _API_SECRET_FIELDS if mp.get(name)), None)
    pass_val = next((mp.get(name) for name in _API_PASS_FIELDS if mp.get(name)), None)
    key_norm = _normalize_str(key_val)
    secret_norm = _normalize_str(secret_val)
    if key_norm and secret_norm:
        return key_norm, secret_norm, _normalize_str(pass_val)
    return None


def _creds_from_object(obj: Any) -> Optional[Tuple[str, str, Optional[str]]]:
    if obj is None:
        return None
    for attr in _API_KEY_FIELDS:
        key_val = getattr(obj, attr, None)
        if key_val:
            break
    else:
        key_val = None
    for attr in _API_SECRET_FIELDS:
        secret_val = getattr(obj, attr, None)
        if secret_val:
            break
    else:
        secret_val = None
    for attr in _API_PASS_FIELDS:
        pass_val = getattr(obj, attr, None)
        if pass_val:
            break
    else:
        pass_val = None
    key_norm = _normalize_str(key_val)
    secret_norm = _normalize_str(secret_val)
    if key_norm and secret_norm:
        return key_norm, secret_norm, _normalize_str(pass_val)
    if hasattr(obj, "to_dict"):
        try:
            maybe = obj.to_dict()  # type: ignore[attr-defined]
        except Exception:
            maybe = None
        if maybe:
            return _creds_from_mapping(maybe)
    if hasattr(obj, "_asdict"):
        try:
            maybe = obj._asdict()  # type: ignore[attr-defined]
        except Exception:
            maybe = None
        if maybe:
            return _creds_from_mapping(maybe)
    return None


def _creds_from_sequence(seq: Any) -> Optional[Tuple[str, str, Optional[str]]]:
    if not isinstance(seq, (list, tuple)) or len(seq) < 2:
        return None
    key_val, secret_val = seq[0], seq[1]
    pass_val = seq[2] if len(seq) > 2 else None
    key_norm = _normalize_str(key_val)
    secret_norm = _normalize_str(secret_val)
    if key_norm and secret_norm:
        return key_norm, secret_norm, _normalize_str(pass_val)
    return None


def _inspect_eoa_api_creds() -> Optional[Tuple[str, str, Optional[str]]]:
    client = get_eoa_client()
    candidates: List[Any] = [
        getattr(client, "api_creds", None),
        getattr(client, "_api_creds", None),
    ]
    getter = getattr(client, "get_api_creds", None)
    if callable(getter):
        try:
            candidates.append(getter())
        except Exception:
            pass
    derive = getattr(client, "create_or_derive_api_creds", None)
    if callable(derive):
        try:
            derived = derive()
        except Exception:
            derived = None
        if derived:
            candidates.append(derived)
    direct_key = getattr(client, "api_key", None)
    direct_secret = getattr(client, "api_secret", None)
    if direct_key and direct_secret:
        candidates.append({"key": direct_key, "secret": direct_secret})
    for cand in candidates:
        if cand is None:
            continue
        parsed = None
        if isinstance(cand, dict):
            parsed = _creds_from_mapping(cand)
        elif isinstance(cand, (list, tuple)):
            parsed = _creds_from_sequence(cand)
        else:
            parsed = _creds_from_object(cand)
        if parsed:
            return parsed
    return None


def _resolve_api_creds() -> Optional[Tuple[str, str, Optional[str]]]:
    env_key = os.getenv("POLY_API_KEY")
    env_secret = os.getenv("POLY_API_SECRET")
    env_pass = os.getenv("POLY_API_PASSPHRASE") or os.getenv("POLY_API_PASS")
    if env_key and env_secret:
        key_clean = env_key.strip()
        secret_clean = env_secret.strip()
        if key_clean and secret_clean:
            pass_clean = env_pass.strip() if env_pass and env_pass.strip() else None
            return key_clean, secret_clean, pass_clean
    try:
        tup = get_api_creds_tuple()
    except Exception:
        tup = (None, None, None)
    key, secret, passphrase = tup
    if key and secret:
        return key, secret, passphrase
    inspected = _inspect_eoa_api_creds()
    if inspected:
        return inspected
    return None


def _describe_auth_context() -> None:
    """打印 EOA API 凭据的摘要，帮助确认脚本运行在 EOA 模式下。"""
    try:
        creds = _resolve_api_creds()
    except Exception as exc:
        print(f"[WARN] 无法派生 API 凭据：{exc}")
        return
    if creds:
        api_key, _, _ = creds
        key_mask = f"{api_key[:6]}***{api_key[-4:]}" if len(api_key) >= 10 else "***"
        print(f"[INFO] 已加载 EOA API credentials：{key_mask}")
    else:
        print("[WARN] 当前未检测到有效的 EOA API credentials，请核对环境变量或私钥配置。")


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _coerce_sequence(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        value = parsed
    if isinstance(value, MappingABC):
        return list(value.values())
    if isinstance(value, IterableABC) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1", "open", "active"}:
            return True
        if lowered in {"false", "no", "n", "0", "closed", "inactive"}:
            return False
    return None


_TIMESTAMP_KEYS = (
    "endDate",
    "endTime",
    "closeDate",
    "closeTime",
    "closedTime",
    "expiry",
    "expirationTime",
    "resolveTime",
    "resolvedTime",
    "finalizationTime",
    "settlementTime",
)


def _parse_timestamp(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return ts
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            ts = float(raw)
        except ValueError:
            ts = None
        if ts is not None:
            if ts > 1e12:
                ts /= 1000.0
            return ts
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
            except ValueError:
                continue
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    return None


@dataclass
class OutcomeSnapshot:
    side: str
    token_id: Optional[str]
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    last_price: Optional[float] = None
    volume_24h: Optional[float] = None
    total_volume: Optional[float] = None
    liquidity: Optional[float] = None

    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        spread = self.best_ask - self.best_bid
        if spread < 0:
            spread = 0.0
        return spread


@dataclass
class MarketSnapshot:
    slug: str
    title: str
    event_slug: Optional[str]
    outcomes: Dict[str, OutcomeSnapshot]
    liquidity: Optional[float]
    volume_24h: Optional[float]
    total_volume: Optional[float]
    hours_to_end: Optional[float]
    is_active: Optional[bool]
    is_resolved: Optional[bool]
    is_closed: Optional[bool]
    tags: Tuple[str, ...] = field(default_factory=tuple)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def yes(self) -> Optional[OutcomeSnapshot]:
        return self.outcomes.get("yes")

    @property
    def no(self) -> Optional[OutcomeSnapshot]:
        return self.outcomes.get("no")


@dataclass
class MarketFilterConfig:
    min_liquidity: float = 0.0
    min_volume_24h: float = 0.0
    min_total_volume: float = 100_000.0
    min_yes_bid: Optional[float] = None
    max_yes_ask: Optional[float] = None
    min_no_bid: Optional[float] = None
    max_no_ask: Optional[float] = None
    max_spread: Optional[float] = None
    min_hours_to_end: Optional[float] = 24.0
    max_end_hours: Optional[float] = 183 * 24.0
    require_active: bool = True
    require_binary: bool = True
    require_trading: bool = False
    require_accepting_orders: bool = True
    min_yes_price: Optional[float] = 0.06
    max_yes_price: Optional[float] = 0.94
    banned_keywords: Tuple[str, ...] = DEFAULT_BANNED_KEYWORDS
    only_keywords: Tuple[str, ...] = ()


@dataclass
class FilterDiagnostics:
    sample_limit: int = 3
    total: int = 0
    passed: int = 0
    failures: Dict[str, int] = field(default_factory=dict)
    samples: Dict[str, List[str]] = field(default_factory=dict)

    def record_pass(self) -> None:
        self.passed += 1

    def record_failure(
        self,
        reason: str,
        *,
        snapshot: Optional[MarketSnapshot] = None,
        market: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.failures[reason] = self.failures.get(reason, 0) + 1
        if self.sample_limit <= 0:
            return
        label = None
        if snapshot is not None:
            label = snapshot.slug or snapshot.title
        elif market is not None:
            label = str(
                market.get("slug")
                or market.get("marketSlug")
                or market.get("question")
                or market.get("title")
                or market.get("name")
                or "(unknown)"
            )
        if not label:
            return
        samples = self.samples.setdefault(reason, [])
        if len(samples) < self.sample_limit and label not in samples:
            samples.append(label)

    def format_report(self, elapsed_seconds: Optional[float] = None) -> str:
        lines: List[str] = []
        if elapsed_seconds is not None:
            lines.append(f"[DIAG] 过滤耗时 {elapsed_seconds:.2f}s。")
        lines.append(
            f"[DIAG] 共评估 {self.total} 个市场，其中 {self.passed} 个通过筛选。"
        )
        failed = self.total - self.passed
        if failed <= 0:
            return "\n".join(lines)
        lines.append(f"[DIAG] 其余 {failed} 个市场未通过筛选，原因分布如下：")
        for reason, count in sorted(self.failures.items(), key=lambda kv: kv[1], reverse=True):
            entry = f"  - {reason}: {count}"
            samples = self.samples.get(reason)
            if samples:
                entry += " （示例: " + ", ".join(samples) + ")"
            lines.append(entry)
        return "\n".join(lines)


def _http_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Optional[Any]:
    params = {k: v for k, v in (params or {}).items() if v is not None}
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }

    # requests 对代理与 TLS 的兼容性更好，优先尝试（与旧版本行为保持一致）。
    try:
        import requests  # type: ignore
    except Exception:
        requests = None  # type: ignore

    if requests is not None:
        try:
            resp = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.JSONDecodeError, ValueError):
            final_url = resp.url if 'resp' in locals() else url  # type: ignore[name-defined]
            print(f"[WARN] 无法解析 JSON：{final_url}")
            return None
        except requests.RequestException as exc:
            print(f"[WARN] 网络请求失败：{exc}")
            # 若是 requests 专属问题（如模块未安装 CA），回退到 urllib

    query = parse.urlencode(params)
    full_url = f"{url}?{query}" if query else url
    req = request.Request(full_url, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                raise error.HTTPError(full_url, resp.status, resp.reason, resp.headers, None)
            raw = resp.read()
            encoding = resp.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip":
                import gzip

                data = gzip.decompress(raw).decode("utf-8")
            elif encoding == "deflate":
                import zlib

                data = zlib.decompress(raw, -zlib.MAX_WBITS).decode("utf-8")
            else:
                data = raw.decode("utf-8")
    except error.HTTPError as exc:
        print(f"[WARN] HTTP {exc.code} {exc.reason} when requesting {full_url}")
        return None
    except error.URLError as exc:
        print(f"[WARN] 网络请求失败：{exc}")
        return None
    except Exception as exc:
        print(f"[WARN] 读取响应失败：{exc}")
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        print(f"[WARN] 无法解析 JSON：{full_url}")
        return None


def fetch_markets(
    *,
    limit: int = 500,
    active_only: bool = True,
    search: Optional[str] = None,
    collection: Optional[str] = None,
    tagged: Optional[str] = None,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"limit": limit}
    if active_only:
        params.update({"active": "true", "closed": "false", "archived": "false"})
    if search:
        params["search"] = search
    if collection:
        params["collection"] = collection
    if tagged:
        params["tag"] = tagged

    payload = _http_json(GAMMA_API_URL, params=params)
    if payload is None:
        return []
    if isinstance(payload, dict) and "data" in payload:
        payload = payload.get("data")
    if not isinstance(payload, list):
        return []
    return [p for p in payload if isinstance(p, dict)]


def _detect_side(name: str, index: int) -> Optional[str]:
    lowered = name.strip().lower()
    if lowered in {"yes", "y", "true", "long", "up"}:
        return "yes"
    if lowered in {"no", "n", "false", "short", "down"}:
        return "no"
    if index == 0:
        return "yes"
    if index == 1:
        return "no"
    return None


def _iter_outcome_dicts(market: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    keys = (
        "outcomes",
        "markets",
        "contracts",
        "outcomeTokens",
        "outcomeTokenPrices",
        "outcomePrices",
    )
    for key in keys:
        val = market.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    yield item
        elif isinstance(val, dict):
            for maybe_dict in val.values():
                if isinstance(maybe_dict, dict):
                    yield maybe_dict


def _summarize_outcomes(market: Dict[str, Any]) -> Dict[str, OutcomeSnapshot]:
    outcomes: Dict[str, OutcomeSnapshot] = {}
    for idx, outcome in enumerate(_iter_outcome_dicts(market)):
        name = str(outcome.get("name") or outcome.get("outcome") or outcome.get("title") or outcome.get("type") or "")
        side = _detect_side(name, idx)
        token_id = outcome.get("tokenId") or outcome.get("clobTokenId") or outcome.get("clob_token_id")
        if token_id is not None:
            token_id = str(token_id)
        best_bid = _coerce_float(
            outcome.get("bestBid")
            or outcome.get("best_bid")
            or outcome.get("bid")
            or outcome.get("bestBidPrice")
        )
        best_ask = _coerce_float(
            outcome.get("bestAsk")
            or outcome.get("best_ask")
            or outcome.get("ask")
            or outcome.get("bestAskPrice")
        )
        last_price = _coerce_float(
            outcome.get("lastPrice")
            or outcome.get("price")
            or outcome.get("last_trade_price")
            or outcome.get("markPrice")
        )
        vol24 = _coerce_float(outcome.get("volume24h") or outcome.get("volume_24h") or outcome.get("volume24Hr"))
        total_vol = _coerce_float(outcome.get("volume") or outcome.get("totalVolume"))
        liquidity = _coerce_float(outcome.get("liquidity") or outcome.get("liquidityNum") or outcome.get("liquidityUsd"))
        if not side and "type" in outcome:
            side = _detect_side(str(outcome.get("type")), idx)
        if not side:
            continue
        outcomes[side] = OutcomeSnapshot(
            side=side,
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            last_price=last_price,
            volume_24h=vol24,
            total_volume=total_vol,
            liquidity=liquidity,
        )
    if {"yes", "no"} <= outcomes.keys():
        return outcomes

    # Fallback：根据 clobTokenIds 补齐 token 信息
    token_ids = market.get("clobTokenIds") or market.get("clobTokens")
    if isinstance(token_ids, (list, tuple)):
        if "yes" not in outcomes and len(token_ids) >= 1:
            outcomes["yes"] = OutcomeSnapshot(side="yes", token_id=str(token_ids[0]))
        if "no" not in outcomes and len(token_ids) >= 2:
            outcomes["no"] = OutcomeSnapshot(side="no", token_id=str(token_ids[1]))

    # 若仍缺少 token id，尝试通过 slug 解析
    slug = market.get("slug") or market.get("marketSlug")
    if slug and ({"yes", "no"} - outcomes.keys()):
        url = f"https://polymarket.com/market/{slug}" if not slug.startswith("http") else slug
        try:
            yes_id, no_id, _title, _raw = resolve_token_ids(url)
        except Exception:
            yes_id = no_id = None
        if yes_id and "yes" not in outcomes:
            outcomes["yes"] = OutcomeSnapshot(side="yes", token_id=str(yes_id))
        if no_id and "no" not in outcomes:
            outcomes["no"] = OutcomeSnapshot(side="no", token_id=str(no_id))

    return outcomes


def _extract_price_entry(payload: Any, *, side: str) -> Optional[float]:
    if side == "bid":
        keys = ("best_bid", "bestBid", "bid", "highest_bid", "highestBid", "buy", "price")
    else:
        keys = (
            "best_ask",
            "bestAsk",
            "ask",
            "offer",
            "best_offer",
            "bestOffer",
            "lowest_ask",
            "lowestAsk",
            "sell",
            "price",
        )

    if isinstance(payload, MappingABC):
        for key in keys:
            if key in payload:
                candidate = _extract_price_entry(payload.get(key), side=side)
                if candidate is not None:
                    return candidate
        for value in payload.values():
            candidate = _extract_price_entry(value, side=side)
            if candidate is not None:
                return candidate
        return None

    if isinstance(payload, IterableABC) and not isinstance(payload, (str, bytes, bytearray)):
        for item in payload:
            candidate = _extract_price_entry(item, side=side)
            if candidate is not None:
                return candidate
        return None

    return _coerce_float(payload)


def _apply_price_fallbacks_from_market(snapshot: MarketSnapshot, market: MappingABC) -> None:
    index_map = {"yes": 0, "no": 1}
    best_bids_seq = _coerce_sequence(market.get("bestBids"))
    best_asks_seq = _coerce_sequence(market.get("bestAsks"))
    outcome_prices_seq = _coerce_sequence(market.get("outcomePrices") or market.get("outcomeTokenPrices"))

    for side, idx in index_map.items():
        outcome = snapshot.outcomes.get(side)
        if not outcome:
            continue
        if outcome.best_bid is None and idx < len(best_bids_seq):
            bid = _extract_price_entry(best_bids_seq[idx], side="bid")
            if bid is not None:
                outcome.best_bid = bid
        if outcome.best_ask is None and idx < len(best_asks_seq):
            ask = _extract_price_entry(best_asks_seq[idx], side="ask")
            if ask is not None:
                outcome.best_ask = ask
        if outcome.last_price is None and idx < len(outcome_prices_seq):
            price = _extract_price_entry(outcome_prices_seq[idx], side="ask")
            if price is not None:
                outcome.last_price = price


def _parse_price_change_for_quotes(pc: MappingABC) -> Tuple[Optional[float], Optional[float]]:
    def _to_float(val: Any) -> Optional[float]:
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    bid_fields = (
        "best_bid",
        "bestBid",
        "bid",
        "best_buy",
        "bestBuy",
        "highest_bid",
        "highestBid",
        "buy",
    )
    ask_fields = (
        "best_ask",
        "bestAsk",
        "ask",
        "offer",
        "sell",
        "best_offer",
        "bestOffer",
        "lowest_ask",
        "lowestAsk",
    )

    best_bid: Optional[float] = None
    for key in bid_fields:
        if key in pc:
            best_bid = _to_float(pc.get(key))
            if best_bid is not None:
                break

    best_ask: Optional[float] = None
    for key in ask_fields:
        if key in pc:
            best_ask = _to_float(pc.get(key))
            if best_ask is not None:
                break

    return best_bid, best_ask


def _fetch_quotes_via_ws(token_ids: Sequence[str], timeout: float = 3.0) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    tokens = [str(tid) for tid in token_ids if tid]
    if not tokens:
        return {}

    try:
        from Volatility_arbitrage_main_ws_EOA import ws_watch_by_ids
    except Exception as exc:
        print(f"[WARN] 无法加载 WS 模块：{exc}")
        return {}

    needed = set(tokens)
    results: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
    lock = threading.Lock()
    stop_event = threading.Event()
    done = threading.Event()

    def _on_event(ev: Dict[str, Any]) -> None:
        if done.is_set():
            return
        if not isinstance(ev, MappingABC):
            return
        pcs = ()
        if ev.get("event_type") == "price_change":
            pcs = ev.get("price_changes") or ()
        elif "price_changes" in ev:
            pcs = ev.get("price_changes") or ()
        for pc in pcs:
            if not isinstance(pc, MappingABC):
                continue
            token_id = str(pc.get("token_id") or pc.get("tokenId") or pc.get("id") or "")
            if token_id not in needed:
                continue
            bid, ask = _parse_price_change_for_quotes(pc)
            if bid is None and ask is None:
                continue
            with lock:
                results[token_id] = (bid, ask)
                if needed.issubset(results.keys()):
                    done.set()
                    stop_event.set()
                    return

    thread = threading.Thread(
        target=ws_watch_by_ids,
        kwargs={
            "asset_ids": tokens,
            "label": "vol-filter-backfill",
            "on_event": _on_event,
            "verbose": False,
            "stop_event": stop_event,
        },
        daemon=True,
    )
    thread.start()

    done.wait(timeout)
    stop_event.set()
    thread.join(timeout=1.0)

    return results


def _maybe_backfill_quotes(snapshot: MarketSnapshot) -> None:
    targets: List[str] = []
    for outcome in (snapshot.yes, snapshot.no):
        if not outcome or not outcome.token_id:
            continue
        if outcome.best_bid is not None and outcome.best_ask is not None:
            continue
        if _ORDERBOOK_CACHE.get(outcome.token_id) is None:
            targets.append(outcome.token_id)

    fetched: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
    if targets:
        fetched = _fetch_quotes_via_ws(targets)
        for token_id, pair in fetched.items():
            _ORDERBOOK_CACHE[token_id] = pair

    for outcome in (snapshot.yes, snapshot.no):
        if not outcome or not outcome.token_id:
            continue
        bid, ask = _ORDERBOOK_CACHE.get(outcome.token_id, (None, None))
        if bid is None or ask is None:
            fetched_pair = fetched.get(outcome.token_id)
            if fetched_pair:
                bid, ask = fetched_pair
        if outcome.best_bid is None and bid is not None:
            outcome.best_bid = bid
        if outcome.best_ask is None and ask is not None:
            outcome.best_ask = ask


def _extract_tags(market: Dict[str, Any]) -> Tuple[str, ...]:
    tags_val = market.get("tags") or market.get("tagNames") or market.get("categories")
    if isinstance(tags_val, str):
        return tuple(t.strip() for t in tags_val.split(",") if t.strip())
    if isinstance(tags_val, (list, tuple)):
        return tuple(str(t) for t in tags_val if isinstance(t, (str, int, float)))
    return tuple()


def build_market_snapshot(market: Dict[str, Any]) -> MarketSnapshot:
    slug = str(market.get("slug") or market.get("marketSlug") or market.get("market_slug") or "")
    title = str(market.get("question") or market.get("title") or market.get("name") or slug)
    event_slug = market.get("eventSlug") or market.get("event_slug") or None
    outcomes = _summarize_outcomes(market)
    liquidity_candidates = (
        market.get("liquidity"),
        market.get("liquidity_num"),
        market.get("liquidityNum"),
        market.get("totalLiquidity"),
        market.get("liquidityUsd"),
        market.get("total_liquidity"),
    )
    liquidity = None
    for candidate in liquidity_candidates:
        liquidity = _coerce_float(candidate)
        if liquidity is not None:
            break
    if liquidity is None:
        liquidity_sum = sum(o.liquidity for o in outcomes.values() if o.liquidity is not None)
        liquidity = liquidity_sum if liquidity_sum > 0 else None

    vol24_candidates = (
        market.get("volume24h"),
        market.get("volume24Hr"),
        market.get("volume24Hour"),
        market.get("volume_24h"),
        market.get("lastDayVolume"),
    )
    volume_24h = None
    for candidate in vol24_candidates:
        volume_24h = _coerce_float(candidate)
        if volume_24h is not None:
            break
    if volume_24h is None:
        volume_sum = sum(o.volume_24h for o in outcomes.values() if o.volume_24h is not None)
        volume_24h = volume_sum if volume_sum > 0 else None

    total_volume_candidates = (
        market.get("volume"),
        market.get("totalVolume"),
        market.get("volume_num"),
        market.get("volumeNum"),
    )
    total_volume = None
    for candidate in total_volume_candidates:
        total_volume = _coerce_float(candidate)
        if total_volume is not None:
            break

    end_ts = None
    for key in _TIMESTAMP_KEYS:
        end_ts = _parse_timestamp(market.get(key))
        if end_ts:
            break
    hours_to_end = None
    if end_ts:
        hours_to_end = (end_ts - time.time()) / 3600.0

    is_active = _coerce_bool(market.get("active"))
    is_resolved = _coerce_bool(market.get("resolved"))
    is_closed = _coerce_bool(market.get("closed"))

    tags = _extract_tags(market)

    snapshot = MarketSnapshot(
        slug=slug,
        title=title,
        event_slug=str(event_slug) if event_slug else None,
        outcomes=outcomes,
        liquidity=liquidity,
        volume_24h=volume_24h,
        total_volume=total_volume,
        hours_to_end=hours_to_end,
        is_active=is_active,
        is_resolved=is_resolved,
        is_closed=is_closed,
        tags=tags,
        raw=market,
    )
    _apply_price_fallbacks_from_market(snapshot, market)
    return snapshot


def _keyword_hit(text: str, keywords: Sequence[str]) -> bool:
    text_low = text.lower()
    for kw in keywords:
        if kw.lower() in text_low:
            return True
    return False


def market_passes_with_reason(
    snapshot: MarketSnapshot, cfg: MarketFilterConfig
) -> Tuple[bool, str]:
    if cfg.require_binary and ({"yes", "no"} - set(snapshot.outcomes.keys())):
        return False, "非二元市场"

    yes = snapshot.yes
    no = snapshot.no

    if cfg.require_trading and (not yes or not no):
        return False, "缺少 YES/NO outcome"
    if cfg.require_trading:
        if yes and yes.best_bid is None and yes.best_ask is None:
            return False, "YES 缺少买卖价"
        if no and no.best_bid is None and no.best_ask is None:
            return False, "NO 缺少买卖价"

    if cfg.require_active:
        if snapshot.is_active is False:
            return False, "非 active 状态"
        if snapshot.is_closed is True:
            return False, "市场已关闭"
    if snapshot.is_resolved:
        return False, "市场已结算"

    if cfg.require_accepting_orders:
        accepting = snapshot.raw.get("acceptingOrders")
        if accepting is False:
            return False, "暂停接单"

    if cfg.min_liquidity > 0:
        if snapshot.liquidity is None or snapshot.liquidity < cfg.min_liquidity:
            return False, "流动性不足"

    if cfg.min_volume_24h > 0:
        if snapshot.volume_24h is None or snapshot.volume_24h < cfg.min_volume_24h:
            return False, "24h 成交量不足"

    if cfg.min_total_volume > 0:
        if snapshot.total_volume is None or snapshot.total_volume < cfg.min_total_volume:
            return False, "总成交量不足"

    if cfg.min_yes_bid is not None:
        if not yes or yes.best_bid is None or yes.best_bid < cfg.min_yes_bid:
            return False, "YES 买价低于阈值"
    if cfg.max_yes_ask is not None:
        if not yes or yes.best_ask is None or yes.best_ask > cfg.max_yes_ask:
            return False, "YES 卖价高于阈值"
    if cfg.min_no_bid is not None:
        if not no or no.best_bid is None or no.best_bid < cfg.min_no_bid:
            return False, "NO 买价低于阈值"
    if cfg.max_no_ask is not None:
        if not no or no.best_ask is None or no.best_ask > cfg.max_no_ask:
            return False, "NO 卖价高于阈值"

    if cfg.max_spread is not None:
        spreads: List[float] = []
        for outcome in (yes, no):
            if not outcome:
                continue
            spread = outcome.spread()
            if spread is not None:
                spreads.append(spread)
        if spreads and max(spreads) > cfg.max_spread:
            return False, "点差超过阈值"

    if snapshot.hours_to_end is not None:
        if snapshot.hours_to_end < 0:
            return False, "市场已过期"
        if cfg.min_hours_to_end is not None and snapshot.hours_to_end < cfg.min_hours_to_end:
            return False, "距离结束时间过短"
        if cfg.max_end_hours is not None and snapshot.hours_to_end > cfg.max_end_hours:
            return False, "距离结束时间过长"
    elif cfg.min_hours_to_end is not None or cfg.max_end_hours is not None:
        return False, "缺少结束时间"

    if cfg.min_yes_price is not None or cfg.max_yes_price is not None:
        yes_price = _resolve_yes_price(snapshot)
        if yes_price is None:
            return False, "缺少 YES 价格"
        if cfg.min_yes_price is not None and yes_price < cfg.min_yes_price:
            return False, "YES 价格低于阈值"
        if cfg.max_yes_price is not None and yes_price > cfg.max_yes_price:
            return False, "YES 价格高于阈值"

    if cfg.banned_keywords and _keyword_hit(snapshot.title, cfg.banned_keywords):
        return False, "命中排除关键字"

    if cfg.only_keywords and not _keyword_hit(snapshot.title, cfg.only_keywords):
        return False, "未命中限定关键字"

    return True, "通过"


def _resolve_yes_price(snapshot: MarketSnapshot) -> Optional[float]:
    yes = snapshot.yes
    if yes:
        for candidate in (yes.best_ask, yes.best_bid, yes.last_price):
            if candidate is not None:
                return candidate

    raw_prices = snapshot.raw.get("outcomePrices") if isinstance(snapshot.raw, dict) else None
    if isinstance(raw_prices, str):
        try:
            raw_prices = json.loads(raw_prices)
        except Exception:
            raw_prices = None
    if isinstance(raw_prices, (list, tuple)) and raw_prices:
        try:
            return float(raw_prices[0])
        except (TypeError, ValueError):
            return None
    return None


def market_passes(snapshot: MarketSnapshot, cfg: MarketFilterConfig) -> bool:
    passed, _ = market_passes_with_reason(snapshot, cfg)
    return passed


def _format_trace_bool(value: Optional[bool]) -> str:
    if value is None:
        return "-"
    return "是" if value else "否"


def _format_trace_numeric(value: Any) -> str:
    numeric = _coerce_float(value)
    if numeric is None:
        if value is None:
            return "-"
        return str(value)
    if abs(numeric) >= 1000:
        return f"{numeric:,.2f}"
    return f"{numeric:.4f}"


def _format_trace_sequence_display(raw: Any, *, limit: int = 4, numeric: bool = False) -> str:
    if numeric:
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                seq: Tuple[Any, ...] = tuple()
            else:
                seq = tuple(parsed) if isinstance(parsed, (list, tuple)) else tuple()
        elif isinstance(raw, (list, tuple)):
            seq = tuple(raw)
        elif raw is None:
            seq = tuple()
        else:
            seq = (raw,)
    elif isinstance(raw, (list, tuple)):
        seq = tuple(raw)
    else:
        if raw is None:
            return "-"
        if isinstance(raw, str):
            text = raw.strip()
            if len(text) > 80:
                text = text[:77] + "..."
            return text or "-"
        return str(raw)
    if not seq:
        return "[]"
    parts: List[str] = []
    for idx, item in enumerate(seq):
        if idx >= limit:
            parts.append("...")
            break
        if numeric:
            parts.append(_format_trace_numeric(item))
        else:
            parts.append(str(item))
    return "[" + ", ".join(parts) + "]"


def _format_trace_raw_market(index: int, total: int, market: Dict[str, Any]) -> str:
    slug = str(
        market.get("slug")
        or market.get("marketSlug")
        or market.get("market_slug")
        or ""
    )
    title = str(
        market.get("question")
        or market.get("title")
        or market.get("name")
        or slug
        or "(no-title)"
    )
    status_line = (
        f"[TRACE] [{index}/{total}] 原始市场：slug={slug or '-'} | 标题={title}\n"
        f"[TRACE]   状态：active={_format_trace_bool(_coerce_bool(market.get('active')))} "
        f"resolved={_format_trace_bool(_coerce_bool(market.get('resolved')))} "
        f"closed={_format_trace_bool(_coerce_bool(market.get('closed')))} "
        f"acceptingOrders={_format_trace_bool(_coerce_bool(market.get('acceptingOrders')))}"
    )

    liquidity_candidates = (
        market.get("liquidity"),
        market.get("liquidity_num"),
        market.get("liquidityNum"),
        market.get("totalLiquidity"),
        market.get("liquidityUsd"),
        market.get("total_liquidity"),
    )
    liquidity = next((val for val in liquidity_candidates if val is not None), None)

    vol24_candidates = (
        market.get("volume24h"),
        market.get("volume24Hr"),
        market.get("volume24Hour"),
        market.get("volume_24h"),
        market.get("lastDayVolume"),
    )
    volume_24h = next((val for val in vol24_candidates if val is not None), None)

    total_volume_candidates = (
        market.get("volume"),
        market.get("totalVolume"),
        market.get("volume_num"),
        market.get("volumeNum"),
    )
    total_volume = next((val for val in total_volume_candidates if val is not None), None)

    price_line = (
        f"[TRACE]   金额：liquidity={_format_trace_numeric(liquidity)} "
        f"volume24h={_format_trace_numeric(volume_24h)} "
        f"totalVolume={_format_trace_numeric(total_volume)}"
    )

    outcome_prices = _format_trace_sequence_display(market.get("outcomePrices"), numeric=True)
    best_bids = _format_trace_sequence_display(market.get("bestBids"), numeric=True)
    best_asks = _format_trace_sequence_display(market.get("bestAsks"), numeric=True)

    quotes_line = (
        f"[TRACE]   报价：outcomePrices={outcome_prices} "
        f"bestBids={best_bids} bestAsks={best_asks}"
    )

    token_ids = market.get("clobTokenIds") or market.get("clobTokens")
    tag_values = market.get("tags") or market.get("tagNames") or market.get("categories")
    misc_line = (
        f"[TRACE]   其他：clobTokenIds={_format_trace_sequence_display(token_ids)} "
        f"tags={_format_trace_sequence_display(tag_values, limit=6)}"
    )

    end_raw = next((market.get(key) for key in _TIMESTAMP_KEYS if market.get(key) is not None), None)
    time_line = f"[TRACE]   时间：raw_end={end_raw or '-'}"

    return "\n".join((status_line, price_line, quotes_line, misc_line, time_line))


def _format_trace_snapshot(snapshot: MarketSnapshot) -> str:
    summary = summarize_market(snapshot)
    indented = "\n".join("[TRACE]     " + line for line in summary.splitlines())
    return "[TRACE]   解析结果：\n" + indented


def filter_markets(
    markets: Iterable[Dict[str, Any]],
    cfg: MarketFilterConfig,
    *,
    diagnostics: Optional[FilterDiagnostics] = None,
    allow_orderbook_backfill: bool = True,
    trace: bool = False,
) -> List[MarketSnapshot]:
    snapshots: List[MarketSnapshot] = []
    market_list = list(markets)
    total = len(market_list)
    for index, market in enumerate(market_list, 1):
        if diagnostics is not None:
            diagnostics.total += 1
        if trace:
            print(_format_trace_raw_market(index, total, market))
        try:
            snapshot = build_market_snapshot(market)
        except Exception as exc:
            print(f"[WARN] 市场解析失败：{exc}")
            if diagnostics is not None:
                reason = f"解析失败({exc.__class__.__name__})"
                diagnostics.record_failure(reason, market=market)
            if trace:
                print(f"[TRACE]   -> 解析失败，跳过（{exc}）。")
            continue
        if cfg.require_trading and allow_orderbook_backfill:
            _maybe_backfill_quotes(snapshot)
        passed, reason = market_passes_with_reason(snapshot, cfg)
        if trace:
            print(_format_trace_snapshot(snapshot))
        if passed:
            if diagnostics is not None:
                diagnostics.record_pass()
            snapshots.append(snapshot)
            if trace:
                print("[TRACE]   -> 结果：通过。")
        elif diagnostics is not None:
            diagnostics.record_failure(reason, snapshot=snapshot)
            if trace:
                print(f"[TRACE]   -> 结果：淘汰（{reason}）。")
        elif trace:
            print(f"[TRACE]   -> 结果：淘汰（{reason}）。")
        if trace:
            print("[TRACE]   --------------------------------------------------")
    return snapshots


def _format_float(value: Optional[float], *, precision: int = 4) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"{value:.{precision}f}"


def summarize_market(snapshot: MarketSnapshot) -> str:
    yes = snapshot.yes or OutcomeSnapshot(side="yes", token_id=None)
    no = snapshot.no or OutcomeSnapshot(side="no", token_id=None)
    ends = "-"
    if snapshot.hours_to_end is not None:
        ends = f"{snapshot.hours_to_end:.1f}h"
    liquidity = _format_float(snapshot.liquidity, precision=0)
    vol24 = _format_float(snapshot.volume_24h, precision=0)
    yes_spread = _format_float(yes.spread(), precision=4)
    no_spread = _format_float(no.spread(), precision=4)
    return (
        f"{snapshot.slug or '(no-slug)'} | {snapshot.title}\n"
        f"  YES[{yes.token_id}] bid={_format_float(yes.best_bid)} ask={_format_float(yes.best_ask)} spread={yes_spread}\n"
        f"  NO [{no.token_id}] bid={_format_float(no.best_bid)} ask={_format_float(no.best_ask)} spread={no_spread}\n"
        f"  liquidity={liquidity}  volume24h={vol24}  ends_in={ends}  tags={','.join(snapshot.tags) or '-'}"
    )


def _load_markets_from_file(path: Path) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[ERROR] 读取本地文件失败：{exc}")
        return []
    if isinstance(payload, dict) and "data" in payload:
        payload = payload["data"]
    if not isinstance(payload, list):
        print("[ERROR] 本地文件格式应为 markets 的数组或 {\"data\": [...]}。")
        return []
    return [p for p in payload if isinstance(p, dict)]


def _cli_build_config(args: argparse.Namespace) -> MarketFilterConfig:
    return MarketFilterConfig(
        min_liquidity=args.min_liquidity,
        min_volume_24h=args.min_volume_24h,
        min_total_volume=args.min_total_volume,
        min_yes_bid=args.min_yes_bid,
        max_yes_ask=args.max_yes_ask,
        min_no_bid=args.min_no_bid,
        max_no_ask=args.max_no_ask,
        max_spread=args.max_spread,
        min_hours_to_end=args.min_end_hours,
        max_end_hours=args.max_end_hours,
        require_active=not args.include_inactive,
        require_binary=not args.allow_non_binary,
        require_trading=not args.allow_illiquid,
        require_accepting_orders=not args.allow_paused,
        min_yes_price=args.min_yes_price,
        max_yes_price=args.max_yes_price,
        banned_keywords=tuple(args.exclude) if args.exclude is not None else DEFAULT_BANNED_KEYWORDS,
        only_keywords=tuple(args.only or ()),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="筛选符合波动率套利条件的 Polymarket 市场（EOA 版）")
    parser.add_argument("--limit", type=int, default=500, help="gamma-api 拉取的市场数量上限（默认 500）")
    parser.add_argument("--search", type=str, help="关键词过滤（由 gamma-api 执行）")
    parser.add_argument("--collection", type=str, help="指定 collection slug 过滤")
    parser.add_argument("--tag", type=str, dest="tagged", help="指定标签过滤")
    parser.add_argument("--from-file", type=Path, help="从本地 JSON 文件加载 markets 数据，跳过网络请求")
    parser.add_argument("--output-json", action="store_true", help="以 JSON 格式输出筛选结果")

    parser.add_argument("--min-liquidity", type=float, default=0.0, help="最小市场流动性（美元）")
    parser.add_argument("--min-volume-24h", type=float, default=0.0, help="过去 24h 最小成交量（美元）")
    parser.add_argument("--min-total-volume", type=float, default=100_000.0, help="最小总成交量（美元）")
    parser.add_argument("--min-yes-bid", type=float, default=None, help="YES 最低买价门槛")
    parser.add_argument("--max-yes-ask", type=float, default=None, help="YES 最高卖价门槛")
    parser.add_argument("--min-no-bid", type=float, default=None, help="NO 最低买价门槛")
    parser.add_argument("--max-no-ask", type=float, default=None, help="NO 最高卖价门槛")
    parser.add_argument("--max-spread", type=float, default=None, help="允许的最大点差（bid/ask 差）")
    parser.add_argument("--min-end-hours", type=float, default=24.0, help="距离结束的最小时数")
    parser.add_argument("--max-end-hours", type=float, default=183 * 24.0, help="距离结束的最大小时数")
    parser.add_argument("--min-yes-price", type=float, default=0.06, help="YES 价格下限")
    parser.add_argument("--max-yes-price", type=float, default=0.94, help="YES 价格上限")

    parser.add_argument("--include-inactive", action="store_true", help="保留 inactive / closed 市场")
    parser.add_argument("--allow-non-binary", action="store_true", help="允许缺失 YES/NO 任一 outcome 的市场")
    parser.add_argument("--allow-illiquid", action="store_true", help="允许无买卖报价的市场")
    parser.add_argument("--allow-paused", action="store_true", help="允许 acceptingOrders=False 的市场")
    parser.add_argument("--skip-orderbook", action="store_true", help="跳过订单簿补全报价，加速诊断")

    parser.add_argument("--exclude", nargs="*", help="标题包含任意关键字则剔除")
    parser.add_argument("--only", nargs="*", help="标题需包含任意关键字方可保留")

    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="输出过滤统计信息，帮助确认被淘汰的原因",
    )
    parser.add_argument(
        "--diagnose-samples",
        type=int,
        default=5,
        help="每种淘汰原因保留的示例数量（默认 5）",
    )
    parser.add_argument(
        "--no-trace",
        action="store_true",
        help="关闭逐市场的详细日志输出",
    )

    args = parser.parse_args(argv)

    cfg = _cli_build_config(args)
    diagnostics = FilterDiagnostics(sample_limit=args.diagnose_samples) if args.diagnose else None

    if args.from_file:
        markets = _load_markets_from_file(args.from_file)
    else:
        _describe_auth_context()
        markets = fetch_markets(
            limit=args.limit,
            active_only=not args.include_inactive,
            search=args.search,
            collection=args.collection,
            tagged=args.tagged,
        )
    if not markets:
        print("[WARN] 未获取到任何市场，请检查网络或输入参数。")
        return 1

    if not args.no_trace:
        print(f"[TRACE] 成功获取 {len(markets)} 个市场，开始逐一解析……")

    start_filter = time.perf_counter()
    snapshots = filter_markets(
        markets,
        cfg,
        diagnostics=diagnostics,
        allow_orderbook_backfill=not args.skip_orderbook,
        trace=not args.no_trace,
    )
    elapsed = time.perf_counter() - start_filter

    if diagnostics is not None:
        print(diagnostics.format_report(elapsed_seconds=elapsed))

    if not snapshots:
        print("[INFO] 没有满足条件的市场。")
        return 0

    if args.output_json:
        serializable = []
        for snap in snapshots:
            serializable.append(
                {
                    "slug": snap.slug,
                    "title": snap.title,
                    "event_slug": snap.event_slug,
                    "liquidity": snap.liquidity,
                    "volume_24h": snap.volume_24h,
                    "hours_to_end": snap.hours_to_end,
                    "yes": snap.yes.__dict__ if snap.yes else None,
                    "no": snap.no.__dict__ if snap.no else None,
                    "tags": list(snap.tags),
                }
            )
        json.dump(serializable, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(f"[RESULT] 共 {len(snapshots)} 个市场符合条件：")
        for snap in snapshots:
            print(summarize_market(snap))
            print("-")

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
