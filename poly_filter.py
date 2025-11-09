# poly_filter.py

import requests
import json
from datetime import datetime, timedelta, timezone

def poly_filter(market):
    if market.get('closed') is True:
        return False
    if market.get('acceptingOrders') is False:
        return False

    try:
        volume = float(market.get('volume', 0) or 0)
    except Exception:
        volume = 0.0
    if volume <= 100000:
        return False

    end_text = (market.get('endDate') or '').strip()
    if len(end_text) < 19:
        return False
    try:
        end_dt = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
    except Exception:
        return False

    now_dt = datetime.now(timezone.utc)
    if end_dt <= now_dt:
        return False
    if not (now_dt + timedelta(hours=24) <= end_dt <= now_dt + timedelta(days=183)):
        return False

    outcome_prices_raw = market.get('outcomePrices', '[]')
    if isinstance(outcome_prices_raw, str):
        try:
            outcome_prices = json.loads(outcome_prices_raw)
        except Exception:
            outcome_prices = []
    else:
        outcome_prices = outcome_prices_raw or []

    try:
        yes_price = float(outcome_prices[0]) if len(outcome_prices) > 0 else None
    except Exception:
        yes_price = None
    if yes_price is None or not (0.06 <= yes_price <= 0.94):
        return False

    q_text    = market.get('question', '') or ''
    group_txt = market.get('groupItemTitle', '') or ''
    slug_txt  = market.get('slug', '') or ''
    desc_txt  = market.get('description', '') or ''
    evs = market.get('events') or []
    ev_title = (evs[0] or {}).get('title', '') if evs else ''
    ev_desc  = (evs[0] or {}).get('description', '') if evs else ''
    haystack = f"{q_text} {group_txt} {ev_title} {slug_txt} {desc_txt} {ev_desc}"

    blacklist = [
        "Bitcoin","BTC","ETH","Ethereum","Sol","Solana","Doge","Dogecoin",
        "BNB","Binance","Cardano","ADA","XRP","Ripple","Matic","Polygon",
        "Crypto","Cryptocurrency","Blockchain","Token","NFT","DeFi",
        "vs","odds","score","spread","moneyline",
        "Esports","CS2","Cup","Arsenal","Liverpool","Chelsea",
        "EPL","PGA","Tour Championship","Scottie Scheffler",
        "Vitality","MOUZ","Falcons","The MongolZ","AL","Houston","Chicago","New York"
    ]
    for w in blacklist:
        if w in haystack:
            return False

    return True


def fetch_markets_by_window(end_min_dt, end_max_dt, window_days=14):
    url = "https://gamma-api.polymarket.com/markets"
    all_markets, seen_ids = [], set()
    cur_start, one_sec = end_min_dt, timedelta(seconds=1)

    while cur_start <= end_max_dt:
        cur_end = min(cur_start + timedelta(days=window_days), end_max_dt)
        params = {
            "limit": 500,
            "order": "endDate",
            "ascending": "true",
            "active": "true",
            "end_date_min": cur_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": cur_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "volume_num_min": 10000,
            "closed": "false",
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            chunk = resp.json()
        except Exception:
            chunk = []

        for m in chunk:
            mid = m.get("id") or m.get("slug")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                all_markets.append(m)

        if len(chunk) >= 500 and window_days > 1:
            sub = fetch_markets_by_window(cur_start, cur_end, window_days=max(1, window_days // 2))
            for m in sub:
                mid = m.get("id") or m.get("slug")
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    all_markets.append(m)

        cur_start = cur_end + one_sec
    return all_markets


def build_market_url(market):
    market_slug = market.get('slug', '')
    events = market.get('events') or []
    event_slug = (events[0] or {}).get('slug') if events else None
    if event_slug and market_slug:
        return f"https://polymarket.com/event/{event_slug}/{market_slug}"
    elif market_slug:
        return f"https://polymarket.com/market/{market_slug}"
    return "https://polymarket.com/markets"


def get_filtered_markets():
    now_utc = datetime.now(timezone.utc)
    end_min_dt = now_utc + timedelta(hours=24)
    end_max_dt = now_utc + timedelta(days=183)

    raw_markets = fetch_markets_by_window(end_min_dt, end_max_dt, window_days=14)
    filtered = []
    for m in raw_markets:
        if not poly_filter(m):
            continue
        outcome_prices_raw = m.get('outcomePrices', '[]')
        if isinstance(outcome_prices_raw, str):
            try:
                outcome_prices = json.loads(outcome_prices_raw)
            except Exception:
                outcome_prices = []
        else:
            outcome_prices = outcome_prices_raw or []

        yes_price = outcome_prices[0] if len(outcome_prices) > 0 else None
        no_price  = outcome_prices[1] if len(outcome_prices) > 1 else None

        # —— clobTokenIds
        token_ids_raw = m.get("clobTokenIds", "[]")
        if isinstance(token_ids_raw, str):
            try:
                token_ids = json.loads(token_ids_raw)
            except Exception:
                token_ids = []
        else:
            token_ids = token_ids_raw or []

        yes_token_id = token_ids[0] if len(token_ids) > 0 else None
        no_token_id  = token_ids[1] if len(token_ids) > 1 else None

        filtered.append({
            "url": build_market_url(m),
            "question": m.get('question', ''),
            "group": m.get('groupItemTitle', ''),
            "yes_price": yes_price,
            "no_price": no_price,
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
            "startDate": m.get('startDate', ''),
            "endDate": m.get('endDate', ''),
            "volume": m.get('volume', 0),
            "description": m.get('description', ''),
            # === 新增字段（仅此处新增） ===
            "orderMinSize": m.get('orderMinSize'),
            "bestAsk": m.get('bestAsk'),
            "bestBid": m.get('bestBid'),
        })
    return filtered


# —— 全局变量（初次加载）
FILTERED_MARKETS = []

def refresh():
    """刷新全局 FILTERED_MARKETS"""
    global FILTERED_MARKETS
    FILTERED_MARKETS = get_filtered_markets()
    return FILTERED_MARKETS

# 初次调用一次
refresh()

if __name__ == "__main__":
    print("通过 poly_filter：", len(FILTERED_MARKETS))
