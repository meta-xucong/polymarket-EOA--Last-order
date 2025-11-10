"""Trading helpers shared across EOA tooling."""

from __future__ import annotations

from collections.abc import Iterable as IterableABC, Mapping as MappingABC
from typing import Any, Dict, Optional, Tuple

__all__ = ["fetch_orderbook_quotes"]

_ORDERBOOK_METHOD_CANDIDATES = (
    ("get_market_orderbook", ("market",)),
    ("get_market_orderbook", ("token_id",)),
    ("get_market_orderbook", ("market_id",)),
    ("get_order_book", ("market",)),
    ("get_order_book", ("token_id",)),
    ("get_order_book", ("market_id",)),
    ("get_orderbook", ("market",)),
    ("get_orderbook", ("token_id",)),
    ("get_orderbook", ("market_id",)),
    ("get_market", ("market",)),
    ("get_market", ("token_id",)),
    ("get_market", ("market_id",)),
    ("get_market_data", ("market",)),
    ("get_market_data", ("token_id",)),
    ("get_market_data", ("market_id",)),
    ("get_ticker", ("market",)),
    ("get_ticker", ("token_id",)),
    ("get_ticker", ("market_id",)),
)


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, float):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _extract_quotes_from_payload(
    payload: Any,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    last_price: Optional[float] = None
    visited: set[int] = set()

    bid_keys = ("best_bid", "bestBid", "bid", "highest_bid", "highestBid", "buy")
    ask_keys = (
        "best_ask",
        "bestAsk",
        "ask",
        "offer",
        "best_offer",
        "bestOffer",
        "lowest_ask",
        "lowestAsk",
        "sell",
    )
    last_keys = (
        "lastPrice",
        "last_price",
        "price",
        "last_trade_price",
        "lastTradePrice",
        "lastTrade",
        "markPrice",
        "close",
    )
    bid_ladder_keys = (
        "bids",
        "bidLadder",
        "buyLadder",
        "bid_ladder",
        "buy_ladder",
        "bidLevels",
        "bid_levels",
    )
    ask_ladder_keys = (
        "asks",
        "askLadder",
        "sellLadder",
        "ask_ladder",
        "sell_ladder",
        "askLevels",
        "ask_levels",
    )

    def _update(bid: Optional[float], ask: Optional[float], last: Optional[float]) -> None:
        nonlocal best_bid, best_ask, last_price
        if bid is not None and best_bid is None:
            best_bid = bid
        if ask is not None and best_ask is None:
            best_ask = ask
        if last is not None and last_price is None:
            last_price = last

    def _walk(obj: Any) -> None:
        if obj is None:
            return
        obj_id = id(obj)
        if obj_id in visited:
            return
        visited.add(obj_id)

        if isinstance(obj, MappingABC):
            direct_bid: Optional[float] = None
            direct_ask: Optional[float] = None
            direct_last: Optional[float] = None

            for key in bid_keys:
                if key in obj:
                    candidate = _coerce_float(obj.get(key))
                    if candidate is not None:
                        direct_bid = candidate
                        break

            for key in ask_keys:
                if key in obj:
                    candidate = _coerce_float(obj.get(key))
                    if candidate is not None:
                        direct_ask = candidate
                        break

            for key in last_keys:
                if key in obj:
                    candidate = _coerce_float(obj.get(key))
                    if candidate is not None:
                        direct_last = candidate
                        break

            _update(direct_bid, direct_ask, direct_last)

            if best_bid is None:
                for key in bid_ladder_keys:
                    if key in obj:
                        ladder_bid = _extract_price_entry(obj.get(key), side="bid")
                        if ladder_bid is not None:
                            _update(ladder_bid, None, None)
                            break

            if best_ask is None:
                for key in ask_ladder_keys:
                    if key in obj:
                        ladder_ask = _extract_price_entry(obj.get(key), side="ask")
                        if ladder_ask is not None:
                            _update(None, ladder_ask, None)
                            break

            for value in obj.values():
                if best_bid is not None and best_ask is not None and last_price is not None:
                    break
                _walk(value)

        elif isinstance(obj, IterableABC) and not isinstance(obj, (str, bytes, bytearray)):
            for item in obj:
                if best_bid is not None and best_ask is not None and last_price is not None:
                    break
                _walk(item)

    _walk(payload)
    return best_bid, best_ask, last_price


def fetch_orderbook_quotes(
    client: Any,
    *,
    token_id: Optional[str] = None,
    market: Optional[str] = None,
    market_id: Optional[str] = None,
    slug: Optional[str] = None,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Retrieve best bid/ask/last quotes using the trading client methods."""

    identifiers: Dict[str, str] = {}
    if token_id:
        token_text = str(token_id)
        identifiers["token_id"] = token_text
    if slug:
        identifiers.setdefault("market", str(slug))
    if market:
        identifiers.setdefault("market", str(market))
    if market_id:
        identifiers["market_id"] = str(market_id)
    if "market" not in identifiers and token_id:
        identifiers["market"] = str(token_id)
    if "market_id" not in identifiers and token_id:
        identifiers["market_id"] = str(token_id)

    for method_name, params in _ORDERBOOK_METHOD_CANDIDATES:
        fn = getattr(client, method_name, None)
        if not callable(fn):
            continue

        kwargs: Dict[str, Any] = {}
        for param in params:
            value = identifiers.get(param)
            if not value:
                kwargs = {}
                break
            kwargs[param] = value
        if not kwargs:
            continue

        try:
            resp = fn(**kwargs)
        except TypeError:
            continue
        except Exception:
            continue

        payload = resp[1] if isinstance(resp, tuple) and len(resp) == 2 else resp
        if isinstance(payload, MappingABC) and "status" in payload and "data" in payload:
            payload = payload.get("data")

        bid, ask, last = _extract_quotes_from_payload(payload)
        if bid is not None or ask is not None or last is not None:
            return bid, ask, last

    return None, None, None

