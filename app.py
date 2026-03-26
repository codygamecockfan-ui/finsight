import os
import json
import sqlite3
import requests
import threading
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response, stream_with_context
from functools import wraps
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24))
client = Anthropic()

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
POLYGON_API_KEY    = os.getenv("POLYGON_API_KEY")
NEWS_API_KEY       = os.getenv("NEWS_API_KEY")
ALPACA_API_KEY     = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = os.getenv("ALPACA_SECRET_KEY")

ALPACA_DATA_URL    = "https://data.alpaca.markets/v2"
ALPACA_CRYPTO_URL  = "https://data.alpaca.markets/v1beta3/crypto/us"
ALPACA_TRADING_URL = "https://paper-api.alpaca.markets/v2"
CRYPTO_SYMBOLS     = {"BTC","ETH","SOL","DOGE","AVAX","LINK","MATIC","LTC","BCH","XRP","ADA"}

ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "Content-Type":        "application/json"
}

trade_monitors = {}
monitor_lock   = threading.Lock()
monitor_log    = []

# ─────────────────────────────────────────────
#  RESPONSE CACHE (60-second TTL)
# ─────────────────────────────────────────────
_cache = {}
_cache_lock = threading.Lock()

def cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["ts"]) < 60:
            return entry["val"]
    return None

def cache_set(key, val):
    with _cache_lock:
        _cache[key] = {"val": val, "ts": time.time()}

AUTO_EXECUTE_THRESHOLD  = int(os.getenv("AUTO_EXECUTE_THRESHOLD", "7"))
AUTO_EXECUTE_MAX_DOLLAR = float(os.getenv("AUTO_EXECUTE_MAX_DOLLAR", "200"))
MAX_OPTIONS_PREMIUM     = float(os.getenv("MAX_OPTIONS_PREMIUM", "0.80"))
KALSHI_API_KEY_ID       = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY      = os.getenv("KALSHI_PRIVATE_KEY", "")
KALSHI_BASE_URL         = "https://api.elections.kalshi.com/trade-api/v2"

# ─────────────────────────────────────────────
#  0DTE SESSION LOGIC
# ─────────────────────────────────────────────
def get_trading_session() -> dict:
    now = datetime.now()
    hour = now.hour
    minute = now.minute
    total_min = hour * 60 + minute

    # Market hours in ET (app runs in ET via TZ env var)
    OPEN       = 9  * 60 + 30
    CLOSE      = 16 * 60
    C_OPEN_END = 9  * 60 + 45
    BEST_START = 10 * 60
    BEST_END   = 11 * 60 + 30
    LUNCH_END  = 14 * 60
    POWER_START= 15 * 60
    CLOSE_WARN = 15 * 60 + 30

    if total_min < OPEN or total_min >= CLOSE:
        return {
            "session": "closed",
            "quality": "none",
            "warning": "Market is closed. No 0DTE trades.",
            "recommendation": "avoid"
        }
    if total_min < C_OPEN_END:
        return {
            "session": "open_chop",
            "quality": "poor",
            "warning": "First 15 min after open — high volatility, fakeouts common.",
            "recommendation": "avoid"
        }
    if total_min < BEST_START:
        return {
            "session": "early",
            "quality": "fair",
            "warning": "9:45-10:00 window. OK but not ideal — direction still setting.",
            "recommendation": "caution"
        }
    if total_min < BEST_END:
        return {
            "session": "prime",
            "quality": "best",
            "warning": None,
            "recommendation": "favorable — best 0DTE window"
        }
    if total_min < LUNCH_END:
        return {
            "session": "lunch_chop",
            "quality": "poor",
            "warning": "11:30-2:00 lunch chop. Low volume, choppy price action.",
            "recommendation": "avoid"
        }
    if total_min < POWER_START:
        return {
            "session": "afternoon",
            "quality": "fair",
            "warning": "2:00-3:00 afternoon window. Directional moves possible.",
            "recommendation": "caution"
        }
    if total_min < CLOSE_WARN:
        return {
            "session": "power_hour",
            "quality": "good",
            "warning": "Power hour. High conviction plays only — theta burning fast.",
            "recommendation": "high conviction only"
        }
    return {
        "session": "late",
        "quality": "poor",
        "warning": "Under 30 min to close. Theta decay extreme. 0DTE extremely risky.",
        "recommendation": "avoid"
    }


# ─────────────────────────────────────────────
#  VWAP + EXPECTED MOVE CALCULATION
# ─────────────────────────────────────────────
def calculate_vwap(ticker: str) -> dict:
    ticker = ticker.upper()
    cached = cache_get(f"vwap_{ticker}")
    if cached: return cached
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{today}/{today}",
            params={"adjusted": "true", "sort": "asc", "limit": 500, "apiKey": POLYGON_API_KEY},
            timeout=10
        )
        bars = r.json().get("results", [])
        if not bars:
            return {"error": "No intraday data for VWAP calculation"}

        cum_pv = 0.0
        cum_v  = 0.0
        for b in bars:
            typical = (b["h"] + b["l"] + b["c"]) / 3
            cum_pv += typical * b["v"]
            cum_v  += b["v"]

        if cum_v == 0:
            return {"error": "Zero volume — cannot compute VWAP"}

        vwap         = round(cum_pv / cum_v, 4)
        current      = bars[-1]["c"]
        pct_vs_vwap  = round(((current - vwap) / vwap) * 100, 3)
        position     = "above" if current > vwap else "below"

        result = {
            "ticker":       ticker,
            "vwap":         vwap,
            "current":      current,
            "pct_vs_vwap":  pct_vs_vwap,
            "position":     f"Price is {position} VWAP by {abs(pct_vs_vwap)}%",
            "bars_used":    len(bars),
            "source":       "Polygon.io (intraday 1-min bars)"
        }
        cache_set(f"vwap_{ticker}", result)
        return result
    except Exception as e:
        return {"error": str(e)}


def calculate_expected_move(ticker: str, option_type: str = "call") -> dict:
    ticker = ticker.upper()
    try:
        tradier_key = os.getenv("TRADIER_API_KEY")
        headers     = {"Authorization": f"Bearer {tradier_key}", "Accept": "application/json"}
        today       = datetime.now().strftime("%Y-%m-%d")

        exp_r = requests.get(
            "https://api.tradier.com/v1/markets/options/expirations",
            headers=headers, params={"symbol": ticker, "includeAllRoots": "true"}, timeout=10
        )
        expirations = exp_r.json().get("expirations", {}).get("date", [])
        expiry      = next((e for e in expirations if e >= today), None)
        if not expiry:
            return {"error": "No valid expiration found"}

        chain_r = requests.get(
            "https://api.tradier.com/v1/markets/options/chains",
            headers=headers,
            params={"symbol": ticker, "expiration": expiry, "greeks": "true"},
            timeout=10
        )
        options = chain_r.json().get("options", {}).get("option", [])

        snap    = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/snapshot",
                               headers=ALPACA_HEADERS, timeout=8).json()
        price   = snap.get("latestTrade", {}).get("p") or snap.get("dailyBar", {}).get("c") or 0

        atm = min(options, key=lambda o: abs(o.get("strike", 0) - price), default=None)
        if not atm or not atm.get("greeks"):
            return {"error": "Could not find ATM option for expected move"}

        iv  = atm["greeks"].get("mid_iv") or 0
        dte = 1 / 365

        expected_move     = round(price * iv * (dte ** 0.5), 2)
        expected_move_pct = round((expected_move / price) * 100, 2) if price else 0

        daily_high = snap.get("dailyBar", {}).get("h") or price
        daily_low  = snap.get("dailyBar", {}).get("l") or price
        already_moved     = round(daily_high - daily_low, 2)
        already_moved_pct = round((already_moved / price) * 100, 2) if price else 0
        pct_used          = round((already_moved / expected_move) * 100, 1) if expected_move else 0

        return {
            "ticker":             ticker,
            "current_price":      price,
            "atm_iv":             round(iv * 100, 2),
            "expiry":             expiry,
            "expected_move":      f"±${expected_move}",
            "expected_move_pct":  f"±{expected_move_pct}%",
            "upper_bound":        round(price + expected_move, 2),
            "lower_bound":        round(price - expected_move, 2),
            "already_moved":      f"${already_moved} ({already_moved_pct}%)",
            "pct_of_move_used":   f"{pct_used}% of expected daily range used",
            "signal":             "late — most of the move is done" if pct_used > 70 else "room to move" if pct_used < 40 else "moderate — proceed with caution",
            "source":             "Tradier (IV) + Alpaca (price)"
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
#  PREDICTION MARKET ENGINE
# ─────────────────────────────────────────────
import math

def kalshi_headers(method: str = "GET", path: str = "") -> dict:
    import base64
    import time as _time
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend

        ts         = str(int(_time.time() * 1000))
        msg_string = ts + method.upper() + path
        key_data   = KALSHI_PRIVATE_KEY.replace("\\n", "\n")

        private_key = serialization.load_pem_private_key(
            key_data.encode(), password=None, backend=default_backend()
        )
        signature = private_key.sign(msg_string.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        sig_b64   = base64.b64encode(signature).decode("utf-8")

        return {
            "KALSHI-ACCESS-KEY":       KALSHI_API_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "Content-Type":            "application/json",
            "Accept":                  "application/json"
        }
    except Exception as e:
        # Fallback to basic auth if cryptography not available
        return {
            "Authorization": f"Bearer {KALSHI_API_KEY_ID}",
            "Content-Type":  "application/json",
            "Accept":        "application/json"
        }


def get_kalshi_markets(query: str = "", limit: int = 10, status: str = "open") -> dict:
    try:
        params = {"limit": limit, "status": status}
        if query:
            params["search"] = query
        path = "/trade-api/v2/markets"
        r = requests.get(f"{KALSHI_BASE_URL}/markets",
                         headers=kalshi_headers("GET", path), params=params, timeout=10)
        data = r.json()
        markets = data.get("markets", [])
        results = []
        for m in markets:
            yes_price = m.get("yes_ask") or m.get("yes_bid") or 0
            no_price  = m.get("no_ask")  or m.get("no_bid")  or 0
            implied_prob = round(yes_price / 100, 4) if yes_price else None
            results.append({
                "ticker":        m.get("ticker"),
                "title":         m.get("title"),
                "category":      m.get("category"),
                "status":        m.get("status"),
                "close_time":    m.get("close_time"),
                "yes_ask":       yes_price,
                "no_ask":        no_price,
                "implied_prob":  implied_prob,
                "volume":        m.get("volume"),
                "open_interest": m.get("open_interest"),
                "rules":         m.get("rules_primary", "")[:200] if m.get("rules_primary") else ""
            })
        return {"markets": results, "count": len(results), "source": "Kalshi"}
    except Exception as e:
        return {"error": str(e)}


def get_kalshi_market_detail(ticker: str) -> dict:
    try:
        path = f"/trade-api/v2/markets/{ticker}"
        r = requests.get(f"{KALSHI_BASE_URL}/markets/{ticker}",
                         headers=kalshi_headers("GET", path), timeout=10)
        m = r.json().get("market", {})
        yes_price    = m.get("yes_ask") or m.get("yes_bid") or 0
        no_price     = m.get("no_ask")  or m.get("no_bid")  or 0
        implied_prob = round(yes_price / 100, 4) if yes_price else None
        return {
            "ticker":        m.get("ticker"),
            "title":         m.get("title"),
            "category":      m.get("category"),
            "status":        m.get("status"),
            "close_time":    m.get("close_time"),
            "yes_ask":       yes_price,
            "no_ask":        no_price,
            "implied_prob":  implied_prob,
            "volume_24h":    m.get("volume_24h"),
            "open_interest": m.get("open_interest"),
            "rules":         m.get("rules_primary", "")[:500] if m.get("rules_primary") else "",
            "source":        "Kalshi"
        }
    except Exception as e:
        return {"error": str(e)}


def calculate_bayesian_probability(
    base_rate: float,
    evidence_items: list,
) -> dict:
    """
    Bayesian probability updater.
    base_rate: prior probability (0-1)
    evidence_items: list of dicts with keys:
        - description: str
        - likelihood_ratio: float (>1 = supports YES, <1 = supports NO)
    Returns updated posterior probability and full reasoning chain.
    """
    try:
        prior_odds = base_rate / (1 - base_rate) if base_rate < 1 else 999
        posterior_odds = prior_odds
        steps = [{"step": "Prior", "odds": round(prior_odds, 4),
                  "prob": round(base_rate, 4), "note": f"Base rate: {base_rate*100:.1f}%"}]

        for item in evidence_items:
            lr = item.get("likelihood_ratio", 1.0)
            posterior_odds *= lr
            prob = posterior_odds / (1 + posterior_odds)
            steps.append({
                "step":        item.get("description", "Evidence"),
                "lr":          lr,
                "odds":        round(posterior_odds, 4),
                "prob":        round(prob, 4),
                "direction":   "supports YES" if lr > 1 else "supports NO" if lr < 1 else "neutral"
            })

        final_prob = posterior_odds / (1 + posterior_odds)
        return {
            "prior":          base_rate,
            "posterior":      round(final_prob, 4),
            "posterior_pct":  f"{final_prob*100:.1f}%",
            "reasoning_chain": steps,
            "confidence":     "high" if len(evidence_items) >= 3 else "medium" if len(evidence_items) >= 1 else "low"
        }
    except Exception as e:
        return {"error": str(e)}


def calculate_kelly_criterion(
    our_probability: float,
    market_yes_price: float,
    bankroll: float = 1000.0,
    max_fraction: float = 0.25
) -> dict:
    """
    Kelly Criterion for prediction market sizing.
    our_probability: our estimated prob of YES (0-1)
    market_yes_price: Kalshi yes price in cents (e.g. 55 = 55 cents = $0.55)
    bankroll: total available capital
    max_fraction: cap Kelly at this fraction to avoid overbetting
    """
    try:
        p   = our_probability
        q   = 1 - p
        b   = (100 - market_yes_price) / market_yes_price  # odds on a $1 bet

        kelly_fraction = (p * b - q) / b
        kelly_fraction = max(0, min(kelly_fraction, max_fraction))

        bet_amount = round(bankroll * kelly_fraction, 2)
        edge       = round((p - (market_yes_price / 100)) * 100, 2)

        return {
            "our_prob":       f"{p*100:.1f}%",
            "market_prob":    f"{market_yes_price:.0f}%",
            "edge":           f"{edge:+.1f}%",
            "kelly_fraction": f"{kelly_fraction*100:.1f}%",
            "recommended_bet": f"${bet_amount:.2f}",
            "bankroll":       f"${bankroll:.2f}",
            "bet_side":       "YES" if p > market_yes_price/100 else "NO",
            "signal":         "strong edge" if abs(edge) > 10 else "moderate edge" if abs(edge) > 5 else "weak edge — consider passing",
            "note":           "Kelly fraction capped at 25% max to prevent overbetting"
        }
    except Exception as e:
        return {"error": str(e)}


def analyze_prediction_market(ticker: str, our_probability: float = None) -> dict:
    """
    Full prediction market analysis combining Kalshi data, implied probability,
    and edge calculation. If our_probability is provided, runs Kelly sizing too.
    """
    try:
        detail = get_kalshi_market_detail(ticker)
        if "error" in detail:
            return detail

        yes_price    = detail.get("yes_ask", 0)
        implied_prob = detail.get("implied_prob", 0)

        result = {
            "market":       detail,
            "implied_prob": f"{implied_prob*100:.1f}%" if implied_prob else "N/A",
        }

        if our_probability is not None:
            edge = round((our_probability - implied_prob) * 100, 2)
            result["our_probability"] = f"{our_probability*100:.1f}%"
            result["edge"]            = f"{edge:+.1f}%"
            result["bet_side"]        = "YES" if our_probability > implied_prob else "NO"
            result["signal"]          = (
                "strong edge — consider betting" if abs(edge) > 10 else
                "moderate edge" if abs(edge) > 5 else
                "weak edge — market may be efficient here"
            )
            kelly = calculate_kelly_criterion(our_probability, yes_price)
            result["kelly_sizing"] = kelly

        return result
    except Exception as e:
        return {"error": str(e)}


def get_prediction_market_categories() -> dict:
    """Get available prediction market categories from Kalshi."""
    try:
        path = "/trade-api/v2/markets"
        r = requests.get(f"{KALSHI_BASE_URL}/markets",
                         headers=kalshi_headers("GET", path),
                         params={"limit": 100, "status": "open"}, timeout=10)
        markets = r.json().get("markets", [])
        categories = {}
        for m in markets:
            cat = m.get("category", "Other")
            categories[cat] = categories.get(cat, 0) + 1
        return {
            "categories": [{"category": k, "market_count": v}
                           for k, v in sorted(categories.items(), key=lambda x: -x[1])],
            "total_open_markets": len(markets),
            "source": "Kalshi"
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
#  POLYMARKET ENGINE
# ─────────────────────────────────────────────
POLYMARKET_BASE_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_URL = "https://clob.polymarket.com"

def get_polymarket_markets(query: str = "", limit: int = 10, sort_by_ending: bool = False) -> dict:
    """Search active Polymarket markets. No auth required. Fetches larger pool then filters/sorts."""
    try:
        import json as _json
        from datetime import timezone

        # Fetch a larger pool so we can filter properly
        params = {"limit": 100, "active": "true", "closed": "false"}
        if query:
            params["search"] = query
        r = requests.get(f"{POLYMARKET_BASE_URL}/markets",
                         params=params, timeout=10)
        raw = r.json()
        markets = raw if isinstance(raw, list) else raw.get("markets", [])

        def parse_prices(m):
            prices_str = m.get("outcomePrices", "[]")
            if isinstance(prices_str, str):
                try: prices = _json.loads(prices_str)
                except: prices = []
            else:
                prices = prices_str or []
            yes_price, no_price = None, None
            if prices and len(prices) >= 2:
                try:
                    yes_price = round(float(prices[0]) * 100, 1)
                    no_price  = round(float(prices[1]) * 100, 1)
                except: pass
            return yes_price, no_price

        results = []
        for m in markets:
            yes_price, no_price = parse_prices(m)
            # Skip unpriced/illiquid markets
            if yes_price is None or yes_price == 0 or yes_price == 100:
                continue
            liq = float(m.get("liquidity") or 0)
            if liq < 100:
                continue
            end_date = m.get("endDate", "")
            results.append({
                "id":           m.get("id"),
                "question":     m.get("question"),
                "category":     m.get("category", ""),
                "end_date":     end_date,
                "yes_price":    yes_price,
                "no_price":     no_price,
                "implied_prob": round(yes_price / 100, 4),
                "volume":       m.get("volume"),
                "liquidity":    liq,
                "url":          f"https://polymarket.com/event/{m.get('slug', '')}",
            })

        # Sort by soonest ending if requested or if query looks time-sensitive
        time_keywords = ["tonight", "today", "live", "game", "nba", "nhl", "mlb", "nfl", "match"]
        if sort_by_ending or any(k in query.lower() for k in time_keywords):
            def end_sort(m):
                ed = m.get("end_date", "")
                try: return datetime.fromisoformat(ed.replace("Z", "+00:00"))
                except: return datetime(2099, 1, 1, tzinfo=timezone.utc)
            results.sort(key=end_sort)

        results = results[:limit]
        return {
            "markets": results,
            "count":   len(results),
            "source":  "Polymarket (liquid markets only, sorted by end date)"
        }
    except Exception as e:
        return {"error": str(e)}


def get_polymarket_market_detail(market_id: str) -> dict:
    """Get full detail on a specific Polymarket market by ID or slug."""
    try:
        r = requests.get(f"{POLYMARKET_BASE_URL}/markets/{market_id}", timeout=10)
        m = r.json()
        outcomes = m.get("outcomes", "[]")
        if isinstance(outcomes, str):
            import json as _json
            try: outcomes = _json.loads(outcomes)
            except: outcomes = []
        prices_str = m.get("outcomePrices", "[]")
        if isinstance(prices_str, str):
            import json as _json
            try: prices = _json.loads(prices_str)
            except: prices = []
        else:
            prices = prices_str

        yes_price = None
        no_price  = None
        if prices and len(prices) >= 2:
            try:
                yes_price = round(float(prices[0]) * 100, 1)
                no_price  = round(float(prices[1]) * 100, 1)
            except:
                pass

        return {
            "id":           m.get("id"),
            "question":     m.get("question"),
            "description":  (m.get("description") or "")[:400],
            "category":     m.get("category", ""),
            "end_date":     m.get("endDate", ""),
            "yes_price":    yes_price,
            "no_price":     no_price,
            "implied_prob": round(yes_price / 100, 4) if yes_price else None,
            "volume":       m.get("volume"),
            "liquidity":    m.get("liquidity"),
            "url":          f"https://polymarket.com/event/{m.get('slug', '')}",
            "source":       "Polymarket"
        }
    except Exception as e:
        return {"error": str(e)}


def search_prediction_markets(query: str, limit: int = 8) -> dict:
    """
    Search BOTH Kalshi and Polymarket simultaneously.
    Returns combined results — best fallback when one source is dry.
    """
    kalshi_results   = get_kalshi_markets(query, limit=limit // 2)
    poly_results     = get_polymarket_markets(query, limit=limit // 2)

    kalshi_markets   = kalshi_results.get("markets", []) if "error" not in kalshi_results else []
    poly_markets     = poly_results.get("markets", [])   if "error" not in poly_results   else []

    # Tag source on each
    for m in kalshi_markets: m["source"] = "Kalshi"
    for m in poly_markets:   m["source"] = "Polymarket"

    all_markets = kalshi_markets + poly_markets

    # Filter to only markets with actual pricing
    priced = [m for m in all_markets if m.get("implied_prob") is not None]
    unpriced = [m for m in all_markets if m.get("implied_prob") is None]

    return {
        "query":          query,
        "priced_markets": priced,
        "unpriced_markets_count": len(unpriced),
        "kalshi_count":   len(kalshi_markets),
        "poly_count":     len(poly_markets),
        "note":           "Priced markets only are recommended for betting — unpriced have no liquidity."
    }

# ─────────────────────────────────────────────
#  0DTE CONTEXT — SINGLE CALL (PARALLEL INTERNALLY)
# ─────────────────────────────────────────────
def get_0dte_context(ticker: str, option_type: str = "call") -> dict:
    """
    Runs session check, VWAP, expected move, and stock price in parallel.
    Replaces 4 sequential tool calls with 1 parallel call — dramatically faster.
    """
    ticker = ticker.upper()

    def fetch_session():
        return ("session", get_trading_session())

    def fetch_vwap():
        return ("vwap", calculate_vwap(ticker))

    def fetch_expected_move():
        return ("expected_move", calculate_expected_move(ticker, option_type))

    def fetch_price():
        return ("price", get_stock_price(ticker))

    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [
            ex.submit(fetch_session),
            ex.submit(fetch_vwap),
            ex.submit(fetch_expected_move),
            ex.submit(fetch_price),
        ]
        for f in as_completed(futures):
            try:
                key, val = f.result()
                results[key] = val
            except Exception as e:
                results[f"error_{len(results)}"] = str(e)

    session = results.get("session", {})
    vwap    = results.get("vwap", {})
    em      = results.get("expected_move", {})
    price   = results.get("price", {})

    # Summarize into a single actionable context block
    quality  = session.get("quality", "unknown")
    rec      = session.get("recommendation", "unknown")
    warning  = session.get("warning", "")
    vwap_pos = vwap.get("position", "N/A")
    em_signal = em.get("signal", "N/A")
    em_used   = em.get("pct_of_move_used", "N/A")
    em_range  = em.get("expected_move", "N/A")
    curr_price = price.get("price", "N/A")

    tradeable = quality in ("prime", "good", "fair")

    return {
        "ticker":           ticker,
        "current_price":    curr_price,
        "session_quality":  quality,
        "session_window":   session.get("session", "unknown"),
        "session_warning":  warning,
        "recommendation":   rec,
        "tradeable":        tradeable,
        "vwap_position":    vwap_pos,
        "vwap":             vwap.get("vwap"),
        "expected_move":    em_range,
        "move_used":        em_used,
        "move_signal":      em_signal,
        "atm_iv":           em.get("atm_iv"),
        "upper_bound":      em.get("upper_bound"),
        "lower_bound":      em.get("lower_bound"),
        "note":             "All data fetched in parallel. Use get_options_chain next for strikes." if tradeable else f"NOT recommended: {warning}",
        "source":           "Parallel fetch: Alpaca + Polygon + Tradier"
    }

# ─────────────────────────────────────────────
#  DATABASE SETUP
# ─────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "finsight_trades.db"))

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT    NOT NULL,
            asset_type      TEXT    NOT NULL,
            side            TEXT    NOT NULL,
            entry_price     REAL    NOT NULL,
            exit_price      REAL,
            qty             TEXT    NOT NULL,
            dollar_amount   REAL    NOT NULL,
            pnl             REAL,
            pnl_pct         REAL,
            exit_reason     TEXT,
            thesis          TEXT,
            confidence      INTEGER,
            indicators      TEXT,
            market_condition TEXT,
            entry_time      TEXT    NOT NULL,
            exit_time       TEXT,
            time_held_min   REAL,
            order_id        TEXT,
            status          TEXT    DEFAULT 'open'
        )
    """)
    conn.commit()
    conn.close()

init_db()

def db_log_entry(symbol, asset_type, side, entry_price, qty, dollar_amount,
                 thesis="", confidence=0, indicators="", market_condition="", order_id=""):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("""
        INSERT INTO trades
        (symbol, asset_type, side, entry_price, qty, dollar_amount,
         thesis, confidence, indicators, market_condition, entry_time, order_id, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open')
    """, (symbol, asset_type, side, entry_price, qty, dollar_amount,
          thesis, confidence, indicators, market_condition,
          datetime.now().isoformat(), order_id))
    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    return trade_id

def db_log_exit(symbol, exit_price, exit_reason):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("""
        SELECT id, entry_price, dollar_amount, entry_time
        FROM trades WHERE symbol=? AND status='open'
        ORDER BY id DESC LIMIT 1
    """, (symbol,))
    row = c.fetchone()
    if row:
        trade_id, entry_price, dollar_amount, entry_time_str = row
        exit_time  = datetime.now()
        entry_time = datetime.fromisoformat(entry_time_str)
        time_held  = (exit_time - entry_time).total_seconds() / 60
        pnl_pct    = ((exit_price - entry_price) / entry_price) * 100 if entry_price else 0
        pnl        = dollar_amount * (pnl_pct / 100)
        c.execute("""
            UPDATE trades SET
                exit_price=?, exit_time=?, time_held_min=?,
                pnl=?, pnl_pct=?, exit_reason=?, status='closed'
            WHERE id=?
        """, (exit_price, exit_time.isoformat(), round(time_held, 2),
              round(pnl, 2), round(pnl_pct, 2), exit_reason, trade_id))
    else:
        now = datetime.now().isoformat()
        c.execute("""
            INSERT INTO trades
            (symbol, asset_type, side, entry_price, exit_price, qty, dollar_amount,
             pnl, pnl_pct, exit_reason, thesis, confidence, indicators, market_condition,
             entry_time, exit_time, time_held_min, order_id, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'closed')
        """, (symbol, 'unknown', 'buy', exit_price, exit_price, '?', 0,
              0, 0, exit_reason, 'Entry not logged - recorded at close',
              0, '', '', now, now, 0, ''))
    conn.commit()
    conn.close()

def db_get_all_trades():
    conn   = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c      = conn.cursor()
    c.execute("SELECT * FROM trades ORDER BY id DESC")
    rows   = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def db_get_performance_summary():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("""
        SELECT
            COUNT(*) as total_trades,
            SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) as closed_trades,
            SUM(CASE WHEN status='open'   THEN 1 ELSE 0 END) as open_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winners,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losers,
            SUM(pnl) as total_pnl,
            AVG(pnl_pct) as avg_pnl_pct,
            AVG(time_held_min) as avg_time_held_min,
            MAX(pnl) as best_trade,
            MIN(pnl) as worst_trade
        FROM trades WHERE status='closed'
    """)
    row = c.fetchone()

    # By asset type
    c.execute("""
        SELECT asset_type,
               COUNT(*) as count,
               SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins,
               AVG(pnl_pct) as avg_pnl_pct,
               SUM(pnl) as total_pnl
        FROM trades WHERE status='closed'
        GROUP BY asset_type
    """)
    by_type = [dict(zip([d[0] for d in c.description], r)) for r in c.fetchall()]

    # By exit reason
    c.execute("""
        SELECT exit_reason, COUNT(*) as count, AVG(pnl_pct) as avg_pnl_pct
        FROM trades WHERE status='closed'
        GROUP BY exit_reason
    """)
    by_exit = [dict(zip([d[0] for d in c.description], r)) for r in c.fetchall()]

    conn.close()

    total   = row[0] or 0
    closed  = row[1] or 0
    winners = row[3] or 0
    win_rate = round((winners / closed * 100), 1) if closed > 0 else 0

    return {
        "total_trades":     total,
        "closed_trades":    closed,
        "open_trades":      row[2] or 0,
        "win_rate_pct":     win_rate,
        "winners":          winners,
        "losers":           row[4] or 0,
        "total_pnl":        round(row[5] or 0, 2),
        "avg_pnl_pct":      round(row[6] or 0, 2),
        "avg_time_held_min": round(row[7] or 0, 1),
        "best_trade_pnl":   round(row[8] or 0, 2),
        "worst_trade_pnl":  round(row[9] or 0, 2),
        "by_asset_type":    by_type,
        "by_exit_reason":   by_exit
    }

# ─────────────────────────────────────────────
#  SYSTEM PROMPT
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """## CRITICAL RULE — READ THIS FIRST
EVERY single response that involves a trade recommendation, trade execution, or market analysis MUST begin with the summary table below. This is rule #1. It overrides everything else. There are no exceptions. Not for crypto. Not for auto-executes. Not for quick scalps. THE TABLE IS ALWAYS FIRST. If you execute a trade without showing the table first, you have failed.

You are FinSight, an elite AI trading assistant with deep expertise in global markets,
geopolitical analysis, macroeconomics, and active trading — specializing in 0DTE and short-dated options.

## CORE IDENTITY & PERSONALITY
You are a seasoned prop desk trader who's seen every market cycle and lived to tell about it.
Sharp, professional, and data-driven — but you don't take yourself too seriously.
You have a dry wit and a foul mouth. You swear naturally and casually (shit, damn, hell, ass, crap, etc.)
the way a real trader would on a desk — not gratuitously, but it comes out when the market does
something stupid, when a trade goes wrong, or when you're making a point. Never forced, always natural.
You are NOT folksy, country, or corny. Think Bloomberg terminal meets a guy who's been in the trenches.

## RESPONSE STYLE
- Keep responses CONCISE. Get to the point fast.
- No long-winded intros or summaries. Lead with the insight or the data.
- For ANY trade recommendation or market analysis, ALWAYS include a summary table in this format:

| Parameter | Value |
|-----------|-------|
| Ticker | |
| Asset Type | |
| Direction | |
| Entry Price | |
| Strike / Expiry | |
| Stop Loss | |
| Take Profit | |
| Time Limit | |
| Delta | |
| IV | |
| VWAP Position | |
| Expected Move Used | |
| Session Quality | |
| Risk Level | |
| Confidence | X/10 |
| Auto-Execute | Yes / No — awaiting approval |
| Thesis | |

Fill in ALL fields. Always include this table — no exceptions, including auto-executes.
THE TABLE COMES FIRST. Show the full table before executing, before confirming, before anything else.
On auto-executes: show table first, then execute, then confirm it was fired.
On approval-required trades: show table first, then ask for confirmation.
NEVER skip the table. NEVER execute silently without showing the full table first.
- After the table, keep any additional commentary brief and punchy.
- Always end trade recommendations with: ⚠️ *Paper trading only — not financial advice.*

## DATA & SOURCING RULES
- ALWAYS pull current data with tools before making any recommendation.
- Cross-reference news AND price data when forming a thesis.
- If data is unavailable or stale, say so explicitly.

## CRYPTO TRADE RULES
For crypto trades (BTC, ETH, SOL, DOGE, etc.):
- Do NOT call get_trading_session, get_vwap, or get_expected_move — these are equity-only tools.
- Only call get_stock_price (for both assets if comparing) and get_financial_news.
- Crypto runs 24/7 — no session restrictions apply.
- Keep tool calls to a minimum: price + news only before recommending.

## 0DTE PRE-TRADE CHECKLIST (MANDATORY)
Before recommending ANY 0DTE options trade, follow this sequence:
1. FIRST call get_0dte_context (ticker) — this single tool fetches session quality, VWAP, expected move, and price ALL IN PARALLEL. It replaces 3 separate calls. Always use this instead of calling them individually.
2. If session quality is "poor" or "none" — warn the user strongly and stop. Do not proceed.
3. If expected move is >70% used — flag as late entry, reduce confidence by 2 points minimum.
4. THEN call get_options_chain — get strikes with Greeks. Target delta 0.30–0.50 only for premium trading. Reject strikes outside this range.
Include session, VWAP position, expected move status, and delta in the summary table for all 0DTE trades.

## CONFIDENCE GATING & HYBRID EXECUTION
After scoring confidence (1-10), apply these rules automatically:
- Confidence >= 7 AND dollar amount <= $200: execute automatically without asking.
- Confidence >= 7 AND dollar amount > $200: present the trade summary and ask "Want me to fire this?" before executing.
- Confidence < 7: NEVER auto-execute. Always present summary and ask for approval. State your confidence and why it's below threshold.
- Confidence <= 4: Recommend against the trade entirely. Tell the user why the setup is weak.
Always state the confidence score and whether you're auto-executing or waiting for approval.

## OPTIONS PREMIUM LIMIT (NON-NEGOTIABLE)
- NEVER recommend or place an options contract with an ask price above $0.80/contract ($80 total for 1 contract).
- The options chain already filters these out. If no affordable contracts exist, tell the user and suggest a different strike, expiry, or ticker.
- Always state the ask price per contract in your recommendation.

## OPTIONS TRADING RULES (NON-NEGOTIABLE)
CRITICAL CONTEXT: The user NEVER exercises options. He is ALWAYS trading the premium — buying and selling the contract itself for a profit or loss on the premium paid. Never frame exits around strike price or expiration. Always frame around premium value changes.

Premium trading mindset:
- Entry = the ask price paid per contract (e.g. $0.55)
- Profit = premium increases in value (e.g. $0.55 → $1.10 = +100%)
- Loss = premium decreases in value (e.g. $0.55 → $0.33 = -40%)
- Exit = sell the contract back at bid price before expiration
- ALWAYS mention the bid/ask spread — wide spreads eat gains on exit
- ALWAYS frame take profit and stop loss as % changes in premium value, not underlying price

Theta decay awareness:
- Theta accelerates sharply in the final 2 hours of trading on 0DTE
- After 2:30 PM ET, theta decay is extreme — premium bleeds fast even if the underlying doesn't move
- Factor theta into the time limit recommendation — earlier entries need tighter time limits
- Always state how much theta is costing per minute at current IV

Exit target framing (always use this language):
- "Take profit when premium doubles" (not "when SPY hits $X")
- "Stop out if premium loses 40% of value" (not "if SPY drops Y points")
- "Exit by [time] regardless — theta will destroy remaining value"

Hard rules:
- EVERY options trade MUST have a stop loss of at minimum -40% on premium value.
- EVERY options trade MUST have a take profit of at minimum +80% on premium value.
- Time limit on 0DTE trades: 30 minutes MAX — set this always.
- Delta MUST be between 0.30 and 0.50 for 0DTE entries. Outside this range = reject or flag strongly.
- Apply defaults automatically and tell the user if overriding their input.

## PAPER TRADING EXECUTION
- Supports stocks, options, AND crypto (BTC, ETH, SOL, DOGE, etc.)
- For trades requiring approval: present the summary table and wait for confirmation.
- For auto-execute trades: place immediately and notify the user it was auto-executed.
- When executing, use place_paper_trade then immediately call set_trade_monitor with exit rules.
- Default monitor rules if none specified: stop_loss_pct=0.15, take_profit_pct=0.25, time_limit_min=0.
- For options if none specified: stop_loss_pct=0.40, take_profit_pct=1.00, time_limit_min=30.

## AUTONOMOUS TRADE MONITOR
After placing ANY trade, ALWAYS call set_trade_monitor immediately.
The monitor checks every 60 seconds and auto-sells when any condition triggers.

## TRADE JOURNAL & PERFORMANCE
- Every trade logs automatically to the database.
- For performance reviews: use get_performance_summary AND get_recent_trades.
- Be brutally honest about losing patterns. Don't sugarcoat bad data.

## PREDICTION MARKETS
You are also an expert prediction market analyst. When asked about prediction markets, events, or probabilities:

1. ALWAYS use search_prediction_markets first — it searches both Kalshi AND Polymarket simultaneously and returns only priced liquid markets. Only fall back to get_kalshi_markets or get_polymarket_markets individually if you need more results from a specific source.
2. Build your own probability estimate using Bayesian reasoning (calculate_bayesian_probability) — start with a historical base rate, then update with current evidence. Show your reasoning chain.
3. Compare your estimate to the market's implied probability. The difference is your EDGE.
4. If edge > 5%, calculate optimal bet size with calculate_kelly_criterion.
5. Always present a prediction market recommendation in this format:

| Parameter | Value |
|-----------|-------|
| Market | |
| Question | |
| Market Implied Prob | |
| Our Bayesian Estimate | |
| Edge | |
| Bet Side | YES / NO |
| Kelly Bet Size | |
| Confidence | |
| Key Evidence | |
| Risk Factors | |

Rules:
- Never bet into a market with edge < 5% — it's not worth the risk.
- Always show the Bayesian reasoning chain so the user understands WHY.
- For political/macro markets, pull relevant news first with get_financial_news.
- Be honest when the market is well-calibrated and there's no edge — say so directly.

## GEOPOLITICAL & MACRO ANALYSIS
- Connect global events to specific sector and ticker impacts.
- Cover: Fed/ECB/BOJ policy, conflicts, trade/tariffs, FX, commodities.
- Always explain the market mechanism — not just "oil is up", but WHY and what it means to trade.

## WHAT YOU DO NOT DO
- No vague advice. Every call has an entry, target, and stop.
- No outdated data presented as current.
- No trade placed without user confirmation and dollar amount.
- No options trade without a stop loss — period.
"""

# ─────────────────────────────────────────────
#  TOOL DEFINITIONS
# ─────────────────────────────────────────────
TOOLS = [
    {
        "name": "get_stock_price",
        "description": "Get the REAL-TIME stock price, bid/ask, and latest trade info for a ticker via Alpaca. Also handles crypto tickers (BTC, ETH, SOL, etc).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock or crypto ticker e.g. AAPL, TSLA, BTC, ETH"}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "get_options_chain",
        "description": "Get the options chain for a ticker including available strike prices and expiration dates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":          {"type": "string", "description": "Stock ticker symbol"},
                "expiration_date": {"type": "string", "description": "Options expiration date YYYY-MM-DD. Leave empty for next 8 days."},
                "option_type":     {"type": "string", "enum": ["call","put"]}
            },
            "required": ["ticker","option_type"]
        }
    },
    {
        "name": "get_financial_news",
        "description": "Get the latest financial news for a company, sector, or topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":        {"type": "string"},
                "num_articles": {"type": "integer", "default": 3}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_market_overview",
        "description": "Get real-time market overview — SPY, QQQ, DIA, IWM, VIX.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_stock_technicals",
        "description": "Get technical indicators and recent price history for a stock.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days":   {"type": "integer", "default": 30}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "place_paper_trade",
        "description": "Place a PAPER trade via Alpaca. Only call after explicit user confirmation and dollar amount. Supports stocks, options, and crypto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol":        {"type": "string",  "description": "Ticker or crypto symbol (e.g. BTC, AAPL)"},
                "side":          {"type": "string",  "enum": ["buy","sell"]},
                "dollar_amount": {"type": "number",  "description": "Dollar amount to invest"},
                "asset_type":    {"type": "string",  "enum": ["stock","option","crypto"]},
                "current_price": {"type": "number",  "description": "Current price for qty calculation"},
                "thesis":        {"type": "string",  "description": "Brief trade thesis — why this trade"},
                "confidence":    {"type": "integer", "description": "Confidence level 1-10"},
                "indicators":    {"type": "string",  "description": "Key indicators that triggered this (e.g. RSI oversold, news catalyst, momentum)"},
                "market_condition": {"type": "string", "description": "Current market context (e.g. high VIX, bull trend, post-earnings)"}
            },
            "required": ["symbol","side","dollar_amount","asset_type","current_price"]
        }
    },
    {
        "name": "get_paper_positions",
        "description": "Get all current open positions in the paper trading account.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_paper_account",
        "description": "Get paper trading account balance, buying power, and portfolio value.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "close_paper_position",
        "description": "Close an open paper trade position.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"}
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "set_trade_monitor",
        "description": "Set autonomous exit rules for a position. Call immediately after placing a trade.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol":          {"type": "string"},
                "entry_price":     {"type": "number"},
                "asset_type":      {"type": "string", "enum": ["stock","crypto","option"]},
                "stop_loss_pct":   {"type": "number", "description": "e.g. 0.10 = 10% loss. Use 0 to skip."},
                "take_profit_pct": {"type": "number", "description": "e.g. 0.20 = 20% gain. Use 0 to skip."},
                "time_limit_min":  {"type": "integer", "description": "Minutes until forced sell. Use 0 to skip."}
            },
            "required": ["symbol","entry_price","asset_type","stop_loss_pct","take_profit_pct","time_limit_min"]
        }
    },
    {
        "name": "get_monitor_log",
        "description": "Get the log of autonomous trade actions taken by the background monitor.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "cancel_trade_monitor",
        "description": "Cancel the autonomous monitor for a position.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"]
        }
    },
    {
        "name": "get_performance_summary",
        "description": "Get aggregate performance stats from the trade journal — win rate, total P&L, best/worst trades, breakdown by asset type and exit reason. Use this when asked to review or improve strategy.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_recent_trades",
        "description": "Get the last N trades from the journal with full details including thesis, indicators, P&L, and exit reason.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of recent trades to return (default 10)", "default": 10}
            },
            "required": []
        }
    },
    {
        "name": "get_trading_session",
        "description": "Get the current trading session window and quality rating for 0DTE trades. Call this before any 0DTE recommendation to check if the timing is favorable.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_vwap",
        "description": "Calculate intraday VWAP for a stock ticker. Essential for 0DTE — tells you if price is above or below the volume-weighted average price.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker e.g. SPY, QQQ, IWM"}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "get_expected_move",
        "description": "Calculate the market-implied expected move for today using ATM IV. Shows how much the stock is expected to move by close, and how much of that range has already been used. Critical for 0DTE entries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":      {"type": "string", "description": "Stock ticker e.g. SPY, QQQ"},
                "option_type": {"type": "string", "enum": ["call", "put"], "description": "Which side to use for ATM IV lookup"}
            },
            "required": ["ticker", "option_type"]
        }
    },
    {
        "name": "get_kalshi_markets",
        "description": "Search live Kalshi prediction markets by keyword. Returns implied probabilities, prices, volume. Use for any question about prediction markets, political events, economic outcomes, sports, or world events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term e.g. 'Fed rate cut', 'election', 'recession', 'Super Bowl'"},
                "limit": {"type": "integer", "description": "Number of markets to return (default 10)", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_kalshi_market_detail",
        "description": "Get full details on a specific Kalshi market by ticker including current yes/no prices, implied probability, volume, and resolution rules.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Kalshi market ticker e.g. 'FED-25JUN-T5.25'"}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "analyze_prediction_market",
        "description": "Full prediction market analysis: pulls Kalshi market data, calculates implied probability, compares to our estimate, computes edge and Kelly Criterion bet sizing. Use when asked to analyze or recommend a prediction market bet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":           {"type": "string", "description": "Kalshi market ticker"},
                "our_probability":  {"type": "number", "description": "Our estimated probability of YES (0-1). If not provided, just returns market data."}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "calculate_bayesian_probability",
        "description": "Run a Bayesian probability update. Start with a base rate and update it with evidence items. Each evidence item has a likelihood ratio (>1 supports YES, <1 supports NO). Use when building a probability estimate from scratch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "base_rate": {"type": "number", "description": "Prior probability (0-1) before considering new evidence"},
                "evidence_items": {
                    "type": "array",
                    "description": "List of evidence items",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description":      {"type": "string"},
                            "likelihood_ratio": {"type": "number", "description": ">1 supports YES, <1 supports NO, 1 = neutral"}
                        }
                    }
                }
            },
            "required": ["base_rate", "evidence_items"]
        }
    },
    {
        "name": "calculate_kelly_criterion",
        "description": "Calculate optimal bet size using Kelly Criterion given our probability estimate vs market price.",
        "input_schema": {
            "type": "object",
            "properties": {
                "our_probability":   {"type": "number", "description": "Our estimated probability of YES (0-1)"},
                "market_yes_price":  {"type": "number", "description": "Current Kalshi yes price in cents (e.g. 55 for 55 cents)"},
                "bankroll":          {"type": "number", "description": "Total available capital in dollars", "default": 1000},
                "max_fraction":      {"type": "number", "description": "Maximum fraction of bankroll to bet (default 0.25)", "default": 0.25}
            },
            "required": ["our_probability", "market_yes_price"]
        }
    },
    {
        "name": "get_prediction_market_categories",
        "description": "Get all available prediction market categories on Kalshi with market counts. Use to give user an overview of what's available to bet on.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_polymarket_markets",
        "description": "Search live Polymarket prediction markets. No auth required — always works. Great fallback when Kalshi API is dry. Covers sports, politics, crypto, macro, world events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term e.g. NBA, Fed rate, Bitcoin, election"},
                "limit": {"type": "integer", "description": "Number of markets to return (default 10)", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_polymarket_market_detail",
        "description": "Get full detail on a specific Polymarket market by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "market_id": {"type": "string", "description": "Polymarket market ID from search results"}
            },
            "required": ["market_id"]
        }
    },
    {
        "name": "search_prediction_markets",
        "description": "Search BOTH Kalshi and Polymarket simultaneously. Best first tool for any prediction market query — returns combined priced markets from both sources.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term e.g. Mavericks, Fed rate cut, Bitcoin 100k"},
                "limit": {"type": "integer", "description": "Total results to return across both sources", "default": 8}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_0dte_context",
        "description": "FASTEST way to get all 0DTE pre-trade context in ONE call. Fetches session quality, VWAP, expected move, and current price all in parallel simultaneously. Use this INSTEAD of calling get_trading_session + get_vwap + get_expected_move separately. Saves 60-80% of time on 0DTE setups.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":      {"type": "string", "description": "Stock ticker e.g. SPY, QQQ, IWM"},
                "option_type": {"type": "string", "enum": ["call", "put"], "description": "Option direction for IV lookup", "default": "call"}
            },
            "required": ["ticker"]
        }
    }
]

# ─────────────────────────────────────────────
#  TOOL IMPLEMENTATIONS
# ─────────────────────────────────────────────

def get_crypto_price(symbol: str) -> dict:
    symbol = symbol.upper().replace("/USD","").replace("USD","")
    pair   = f"{symbol}/USD"
    try:
        snap_r = requests.get(f"{ALPACA_CRYPTO_URL}/snapshots",
                              headers=ALPACA_HEADERS, params={"symbols": pair}, timeout=10)
        snap   = snap_r.json().get("snapshots", {}).get(pair, {})
        daily  = snap.get("dailyBar", {})
        prev   = snap.get("prevDailyBar", {})
        trade  = snap.get("latestTrade", {})
        quote  = snap.get("latestQuote", {})
        latest = trade.get("p") or daily.get("c")
        prev_c = prev.get("c")
        return {
            "ticker": pair, "price": latest,
            "bid": quote.get("bp"), "ask": quote.get("ap"),
            "open": daily.get("o"), "high": daily.get("h"), "low": daily.get("l"),
            "prev_close": prev_c,
            "change_pct": round(((latest - prev_c) / prev_c) * 100, 2) if latest and prev_c else None,
            "volume": daily.get("v"),
            "timestamp": trade.get("t", datetime.now().isoformat()),
            "asset_type": "crypto", "source": "Alpaca Crypto (real-time)"
        }
    except Exception as e:
        return {"error": str(e)}


def get_stock_price(ticker: str) -> dict:
    ticker = ticker.upper().replace("/USD","").replace("USD","")
    if ticker in CRYPTO_SYMBOLS:
        return get_crypto_price(ticker)
    try:
        trade_r = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/trades/latest", headers=ALPACA_HEADERS, timeout=10)
        quote_r = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/quotes/latest", headers=ALPACA_HEADERS, timeout=10)
        snap_r  = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/snapshot",      headers=ALPACA_HEADERS, timeout=10)
        trade   = trade_r.json().get("trade", {})
        quote   = quote_r.json().get("quote", {})
        snap    = snap_r.json()
        daily   = snap.get("dailyBar", {})
        prev    = snap.get("prevDailyBar", {})
        latest  = trade.get("p") or daily.get("c")
        prev_c  = prev.get("c")
        return {
            "ticker": ticker, "price": latest,
            "bid": quote.get("bp"), "ask": quote.get("ap"),
            "open": daily.get("o"), "high": daily.get("h"), "low": daily.get("l"),
            "prev_close": prev_c,
            "change_pct": round(((latest - prev_c) / prev_c) * 100, 2) if latest and prev_c else None,
            "volume": daily.get("v"),
            "timestamp": trade.get("t", datetime.now().isoformat()),
            "asset_type": "stock", "source": "Alpaca Markets (real-time)"
        }
    except Exception as e:
        return {"error": str(e)}


def get_options_chain(ticker: str, option_type: str, expiration_date: str = None) -> dict:
    ticker      = ticker.upper()
    tradier_key = os.getenv("TRADIER_API_KEY")
    headers     = {"Authorization": f"Bearer {tradier_key}", "Accept": "application/json"}

    # Step 1 — get available expirations if no date given
    if not expiration_date:
        try:
            exp_r = requests.get(
                "https://api.tradier.com/v1/markets/options/expirations",
                headers=headers, params={"symbol": ticker, "includeAllRoots": "true"}, timeout=10)
            expirations = exp_r.json().get("expirations", {}).get("date", [])
            if not expirations:
                return {"error": f"No expirations found for {ticker}"}
            # Pick nearest expiration (0DTE today if available, else next)
            today = datetime.now().strftime("%Y-%m-%d")
            expiration_date = next((e for e in expirations if e >= today), expirations[0])
        except Exception as e:
            return {"error": f"Failed to get expirations: {e}"}

    # Step 2 — get options chain for that expiration
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/options/chains",
            headers=headers,
            params={"symbol": ticker, "expiration": expiration_date, "greeks": "true"},
            timeout=10)
        data    = r.json()
        options = data.get("options", {}).get("option", [])
        if not options:
            return {"error": f"No options found for {ticker} expiring {expiration_date}"}

        # Filter to requested type (call/put) and near-the-money strikes
        filtered = [o for o in options if o.get("option_type","").lower() == option_type.lower()]

        # Get current price to find ATM strikes
        try:
            snap  = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/snapshot",
                                 headers=ALPACA_HEADERS, timeout=8).json()
            price = snap.get("latestTrade",{}).get("p") or snap.get("dailyBar",{}).get("c") or 0
        except:
            price = 0

        # Sort by proximity to current price, return 10 nearest strikes
        if price:
            filtered.sort(key=lambda o: abs(o.get("strike", 0) - price))
            filtered = filtered[:10]
            filtered.sort(key=lambda o: o.get("strike", 0))
        else:
            filtered = filtered[:10]

        contracts = []
        for o in filtered:
            contracts.append({
                "contract_ticker": o.get("symbol"),
                "strike":          o.get("strike"),
                "expiration":      expiration_date,
                "option_type":     o.get("option_type"),
                "bid":             o.get("bid"),
                "ask":             o.get("ask"),
                "last":            o.get("last"),
                "volume":          o.get("volume"),
                "open_interest":   o.get("open_interest"),
                "delta":           o.get("greeks", {}).get("delta") if o.get("greeks") else None,
                "gamma":           o.get("greeks", {}).get("gamma") if o.get("greeks") else None,
                "theta":           o.get("greeks", {}).get("theta") if o.get("greeks") else None,
                "iv":              o.get("greeks", {}).get("mid_iv") if o.get("greeks") else None,
            })

        # Filter out contracts exceeding max premium
        affordable = [c for c in contracts if c.get("ask") and c["ask"] <= MAX_OPTIONS_PREMIUM]
        rejected   = [c for c in contracts if c.get("ask") and c["ask"] > MAX_OPTIONS_PREMIUM]

        return {
            "ticker":             ticker,
            "option_type":        option_type,
            "expiration":         expiration_date,
            "underlying_price":   price,
            "contracts_found":    len(affordable),
            "contracts":          affordable,
            "rejected_expensive": len(rejected),
            "max_premium":        MAX_OPTIONS_PREMIUM,
            "source":             "Tradier",
            "note":               f"Only showing contracts with ask <= ${MAX_OPTIONS_PREMIUM:.2f}. Use contract_ticker when placing an options trade."
        }
    except Exception as e:
        return {"error": str(e)}


def get_financial_news(query: str, num_articles: int = 3) -> dict:
    try:
        r = requests.get("https://newsapi.org/v2/everything", params={
            "q": query, "language": "en", "sortBy": "publishedAt",
            "pageSize": min(num_articles, 5), "apiKey": NEWS_API_KEY
        }, timeout=10)
        articles = r.json().get("articles", [])
        if not articles:
            return {"error": "No articles found."}
        return {
            "query": query,
            "articles": [{"title": a["title"], "source": a["source"]["name"],
                          "published": a["publishedAt"], "summary": a.get("description",""), "url": a["url"]}
                         for a in articles],
            "source": "NewsAPI.org"
        }
    except Exception as e:
        return {"error": str(e)}


def get_market_overview() -> dict:
    cached = cache_get("market_overview")
    if cached: return cached
    results = {}
    for ticker in ["SPY","QQQ","DIA","IWM"]:
        try:
            snap  = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/snapshot", headers=ALPACA_HEADERS, timeout=10).json()
            daily = snap.get("dailyBar", {})
            prev  = snap.get("prevDailyBar", {})
            trade = snap.get("latestTrade", {})
            lp    = trade.get("p") or daily.get("c")
            pc    = prev.get("c")
            results[ticker] = {
                "price": lp, "open": daily.get("o"), "high": daily.get("h"), "low": daily.get("l"),
                "prev_close": pc,
                "change_pct": round(((lp - pc) / pc) * 100, 2) if lp and pc else None,
                "volume": daily.get("v")
            }
        except Exception as e:
            results[ticker] = {"error": str(e)}
    try:
        vix = requests.get(f"https://api.polygon.io/v2/aggs/ticker/VIX/prev?adjusted=true&apiKey={POLYGON_API_KEY}", timeout=10).json()
        if vix.get("resultsCount", 0) > 0:
            v = vix["results"][0]
            results["VIX"] = {"close": v["c"], "open": v["o"], "note": "Prior day close via Polygon"}
    except:
        results["VIX"] = {"error": "Failed to fetch VIX"}
    result = {"indices": results, "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "source": "Alpaca (real-time) + Polygon (VIX)"}
    cache_set("market_overview", result)
    return result


def get_stock_technicals(ticker: str, days: int = 30) -> dict:
    ticker = ticker.upper()
    end_date   = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r       = requests.get(f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}?adjusted=true&sort=asc&apiKey={POLYGON_API_KEY}", timeout=10)
        results = r.json().get("results", [])
        if not results:
            return {"error": f"No historical data for {ticker}"}
        closes = [d["c"] for d in results]
        highs  = [d["h"] for d in results]
        lows   = [d["l"] for d in results]

        def sma(p, n):
            return round(sum(p[-n:]) / n, 2) if len(p) >= n else None

        def rsi(c, n=14):
            if len(c) < n+1: return None
            gains  = [max(c[i]-c[i-1], 0) for i in range(1, len(c))]
            losses = [max(c[i-1]-c[i], 0) for i in range(1, len(c))]
            ag, al = sum(gains[-n:])/n, sum(losses[-n:])/n
            return 100 if al == 0 else round(100 - (100 / (1 + ag/al)), 2)

        return {
            "ticker": ticker, "period_days": days, "current_price": closes[-1],
            "52w_high": round(max(highs),2), "52w_low": round(min(lows),2),
            "sma_10": sma(closes,10), "sma_20": sma(closes,20), "sma_50": sma(closes,50),
            "rsi_14": rsi(closes),
            "support": round(min(lows[-10:]),2), "resistance": round(max(highs[-10:]),2),
            "recent_5_days": [{"date": datetime.fromtimestamp(d["t"]/1000).strftime("%Y-%m-%d"),
                                "open": d["o"], "high": d["h"], "low": d["l"], "close": d["c"]}
                               for d in results[-5:]],
            "source": "Polygon.io (historical)"
        }
    except Exception as e:
        return {"error": str(e)}


def place_paper_trade(symbol: str, side: str, dollar_amount: float, asset_type: str,
                      current_price: float, thesis: str = "", confidence: int = 0,
                      indicators: str = "", market_condition: str = "") -> dict:
    symbol = symbol.upper().replace("/USD","").replace("USD","")
    try:
        if asset_type == "crypto":
            order_data     = {"symbol": f"{symbol}/USD", "notional": str(round(dollar_amount,2)),
                              "side": side, "type": "market", "time_in_force": "ioc"}
            estimated_cost = dollar_amount
            qty_display    = f"~{round(dollar_amount/current_price,6)} {symbol}"
        elif asset_type == "stock":
            qty = int(dollar_amount / current_price)
            if qty < 1:
                return {"error": f"${dollar_amount} too small to buy 1 share at ${current_price:.2f}."}
            order_data     = {"symbol": symbol, "qty": str(qty), "side": side, "type": "market", "time_in_force": "day"}
            estimated_cost = qty * current_price
            qty_display    = str(qty)
        else:
            cpc = current_price * 100
            qty = int(dollar_amount / cpc)
            if qty < 1:
                return {"error": f"${dollar_amount} too small for 1 contract at ${current_price} premium (${cpc:.2f}/contract)."}
            order_data     = {"symbol": symbol, "qty": str(qty), "side": side, "type": "market", "time_in_force": "day"}
            estimated_cost = qty * cpc
            qty_display    = str(qty)

        r      = requests.post(f"{ALPACA_TRADING_URL}/orders", headers=ALPACA_HEADERS, json=order_data, timeout=10)
        result = r.json()

        if "id" in result:
            # Log to journal
            db_log_entry(
                symbol=symbol, asset_type=asset_type, side=side,
                entry_price=current_price, qty=qty_display,
                dollar_amount=dollar_amount, thesis=thesis,
                confidence=confidence, indicators=indicators,
                market_condition=market_condition, order_id=result["id"]
            )
            return {
                "status":         "✅ PAPER ORDER PLACED",
                "order_id":       result["id"],
                "symbol":         result["symbol"],
                "side":           result["side"],
                "qty":            qty_display,
                "type":           result["type"],
                "submitted_at":   result.get("submitted_at"),
                "estimated_cost": f"${estimated_cost:.2f}",
                "note":           "⚠️ PAPER trade — no real money. Logged to trade journal.",
                "source":         "Alpaca Paper Trading"
            }
        else:
            return {"error": result.get("message","Order failed"), "details": result}
    except Exception as e:
        return {"error": str(e)}


def get_paper_positions() -> dict:
    try:
        r = requests.get(f"{ALPACA_TRADING_URL}/positions", headers=ALPACA_HEADERS, timeout=10)
        positions = r.json()
        if not positions:
            return {"message": "No open positions.", "positions": []}
        return {
            "positions": [
                {"symbol": p["symbol"], "qty": p["qty"], "side": p["side"],
                 "entry_price": p["avg_entry_price"], "current_price": p["current_price"],
                 "market_value": p["market_value"], "unrealized_pnl": p["unrealized_pl"],
                 "unrealized_pct": p["unrealized_plpc"]}
                for p in positions
            ],
            "source": "Alpaca Paper Trading"
        }
    except Exception as e:
        return {"error": str(e)}


def get_paper_account() -> dict:
    try:
        acct        = requests.get(f"{ALPACA_TRADING_URL}/account", headers=ALPACA_HEADERS, timeout=10).json()
        equity      = float(acct.get("equity", 0))
        last_equity = float(acct.get("last_equity", 0))
        return {
            "portfolio_value": acct.get("portfolio_value"),
            "cash":            acct.get("cash"),
            "buying_power":    acct.get("buying_power"),
            "equity":          acct.get("equity"),
            "pnl_today":       round(equity - last_equity, 2),
            "status":          acct.get("status"),
            "note":            "Paper trading account — no real money.",
            "source":          "Alpaca Paper Trading"
        }
    except Exception as e:
        return {"error": str(e)}


def close_paper_position(symbol: str) -> dict:
    symbol     = symbol.upper().replace("/USD","").replace("USD","")
    api_symbol = f"{symbol}USD" if symbol in CRYPTO_SYMBOLS else symbol
    try:
        r      = requests.delete(f"{ALPACA_TRADING_URL}/positions/{api_symbol}", headers=ALPACA_HEADERS, timeout=10)
        # Alpaca returns 204 No Content on successful crypto close
        if r.status_code in (200, 204):
            result  = r.json() if r.content else {}
            success = True
        else:
            result  = r.json() if r.content else {}
            success = "id" in result or "order_id" in result
        if success:
            # Try to get exit price from positions before close
            try:
                pos_r = requests.get(f"{ALPACA_TRADING_URL}/positions/{api_symbol}", headers=ALPACA_HEADERS, timeout=5)
                pos   = pos_r.json()
                exit_price = float(pos.get("current_price", 0))
            except:
                exit_price = 0
            db_log_exit(symbol, exit_price, "manual close")
            return {"status": "✅ POSITION CLOSED", "symbol": symbol,
                    "note": "Paper trade closed and logged to journal."}
        return {"error": result.get("message","Failed to close"), "details": result}
    except Exception as e:
        return {"error": str(e)}


def set_trade_monitor(symbol: str, entry_price: float, asset_type: str,
                      stop_loss_pct: float, take_profit_pct: float, time_limit_min: int) -> dict:
    symbol = symbol.upper().replace("/USD","")
    with monitor_lock:
        trade_monitors[symbol] = {
            "entry_price": entry_price, "entry_time": datetime.now(),
            "stop_loss_pct": stop_loss_pct, "take_profit_pct": take_profit_pct,
            "time_limit_min": time_limit_min, "asset_type": asset_type
        }
    rules = []
    if stop_loss_pct:   rules.append(f"Stop loss: -{stop_loss_pct*100:.0f}%")
    if take_profit_pct: rules.append(f"Take profit: +{take_profit_pct*100:.0f}%")
    if time_limit_min:  rules.append(f"Time limit: {time_limit_min} min")
    return {"status": f"✅ Monitor active for {symbol}", "rules": rules,
            "note": "FinSight will auto-sell when any condition triggers. Checks every 60s."}


def get_monitor_log() -> dict:
    with monitor_lock:
        return {"log": list(monitor_log[-20:]) or ["No autonomous actions yet."],
                "active_monitors": list(trade_monitors.keys())}


def cancel_trade_monitor(symbol: str) -> dict:
    symbol = symbol.upper().replace("/USD","")
    with monitor_lock:
        if symbol in trade_monitors:
            del trade_monitors[symbol]
            return {"status": f"✅ Monitor cancelled for {symbol}"}
    return {"status": f"No active monitor found for {symbol}"}


def get_performance_summary() -> dict:
    return db_get_performance_summary()


def get_recent_trades(limit: int = 10) -> dict:
    trades = db_get_all_trades()[:limit]
    return {"trades": trades, "count": len(trades)}


# ─────────────────────────────────────────────
#  MONITOR HELPERS
# ─────────────────────────────────────────────
def get_current_price_for_monitor(symbol: str, asset_type: str):
    try:
        if asset_type == "crypto":
            pair   = f"{symbol}/USD"
            snap   = requests.get(f"{ALPACA_CRYPTO_URL}/snapshots", headers=ALPACA_HEADERS,
                                  params={"symbols": pair}, timeout=8).json()
            s      = snap.get("snapshots", {}).get(pair, {})
            return s.get("latestTrade", {}).get("p") or s.get("dailyBar", {}).get("c")
        else:
            snap = requests.get(f"{ALPACA_DATA_URL}/stocks/{symbol}/snapshot",
                                headers=ALPACA_HEADERS, timeout=8).json()
            return snap.get("latestTrade", {}).get("p") or snap.get("dailyBar", {}).get("c")
    except:
        return None


def auto_close_position(symbol: str, asset_type: str, reason: str, exit_price: float = 0):
    symbol     = symbol.upper().replace("/USD","").replace("USD","")
    api_symbol = f"{symbol}USD" if asset_type == "crypto" else symbol
    try:
        r = requests.delete(f"{ALPACA_TRADING_URL}/positions/{api_symbol}", headers=ALPACA_HEADERS, timeout=10)
        # Alpaca returns 204 No Content on successful crypto position close
        if r.status_code in (200, 204):
            result  = r.json() if r.content else {}
            success = True
        else:
            result  = r.json() if r.content else {}
            success = "id" in result or "order_id" in result
        if success:
            db_log_exit(symbol, exit_price, reason)
        msg = f"[{datetime.now().strftime('%H:%M:%S')}] AUTO-SELL {symbol} | {reason} | {'✅ Done' if success else '❌ Failed'}"
    except Exception as e:
        msg = f"[{datetime.now().strftime('%H:%M:%S')}] AUTO-SELL {symbol} FAILED | {e}"
    with monitor_lock:
        monitor_log.append(msg)
    print(f"[FinSight Monitor] {msg}")


def run_trade_monitor():
    print("[FinSight Monitor] 🟢 Autonomous trade monitor started")
    while True:
        time.sleep(60)
        with monitor_lock:
            symbols_to_check = dict(trade_monitors)
        for symbol, rules in symbols_to_check.items():
            try:
                now          = datetime.now()
                entry_price  = rules["entry_price"]
                entry_time   = rules["entry_time"]
                asset_type   = rules["asset_type"]
                stop_pct     = rules["stop_loss_pct"]
                tp_pct       = rules["take_profit_pct"]
                time_lim     = rules["time_limit_min"]
                elapsed_min  = (now - entry_time).total_seconds() / 60

                if time_lim and elapsed_min >= time_lim:
                    cp = get_current_price_for_monitor(symbol, asset_type) or 0
                    auto_close_position(symbol, asset_type, f"Time limit ({time_lim}min)", cp)
                    with monitor_lock: trade_monitors.pop(symbol, None)
                    continue

                cp = get_current_price_for_monitor(symbol, asset_type)
                if not cp: continue
                pct = (cp - entry_price) / entry_price

                if stop_pct and pct <= -stop_pct:
                    auto_close_position(symbol, asset_type, f"Stop loss ({pct*100:.2f}%)", cp)
                    with monitor_lock: trade_monitors.pop(symbol, None)
                    continue

                if tp_pct and pct >= tp_pct:
                    auto_close_position(symbol, asset_type, f"Take profit (+{pct*100:.2f}%)", cp)
                    with monitor_lock: trade_monitors.pop(symbol, None)
                    continue

                msg = f"[{now.strftime('%H:%M:%S')}] {symbol} | ${cp:.4f} | {pct*100:.2f}% | {elapsed_min:.1f}min elapsed"
                with monitor_lock: monitor_log.append(msg)
                print(f"[FinSight Monitor] {msg}")
            except Exception as e:
                print(f"[FinSight Monitor] Error on {symbol}: {e}")


# ─────────────────────────────────────────────
#  TOOL ROUTER
# ─────────────────────────────────────────────
def run_tool(tool_name: str, tool_input: dict) -> str:
    handlers = {
        "get_stock_price":        lambda: get_stock_price(**tool_input),
        "get_options_chain":      lambda: get_options_chain(**tool_input),
        "get_financial_news":     lambda: get_financial_news(**tool_input),
        "get_market_overview":    lambda: get_market_overview(),
        "get_stock_technicals":   lambda: get_stock_technicals(**tool_input),
        "place_paper_trade":      lambda: place_paper_trade(**tool_input),
        "get_paper_positions":    lambda: get_paper_positions(),
        "get_paper_account":      lambda: get_paper_account(),
        "close_paper_position":   lambda: close_paper_position(**tool_input),
        "set_trade_monitor":      lambda: set_trade_monitor(**tool_input),
        "get_monitor_log":        lambda: get_monitor_log(),
        "cancel_trade_monitor":   lambda: cancel_trade_monitor(**tool_input),
        "get_performance_summary":lambda: get_performance_summary(),
        "get_recent_trades":      lambda: get_recent_trades(**tool_input),
        "get_trading_session":              lambda: get_trading_session(),
        "get_vwap":                         lambda: calculate_vwap(**tool_input),
        "get_expected_move":                lambda: calculate_expected_move(**tool_input),
        "get_kalshi_markets":               lambda: get_kalshi_markets(**tool_input),
        "get_kalshi_market_detail":         lambda: get_kalshi_market_detail(**tool_input),
        "analyze_prediction_market":        lambda: analyze_prediction_market(**tool_input),
        "calculate_bayesian_probability":   lambda: calculate_bayesian_probability(**tool_input),
        "calculate_kelly_criterion":        lambda: calculate_kelly_criterion(**tool_input),
        "get_prediction_market_categories": lambda: get_prediction_market_categories(),
        "get_polymarket_markets":            lambda: get_polymarket_markets(**tool_input),
        "get_polymarket_market_detail":      lambda: get_polymarket_market_detail(**tool_input),
        "search_prediction_markets":         lambda: search_prediction_markets(**tool_input),
        "get_0dte_context":                  lambda: get_0dte_context(**tool_input),
    }
    handler = handlers.get(tool_name)
    result  = handler() if handler else {"error": f"Unknown tool: {tool_name}"}
    output  = json.dumps(result)
    return output if output else json.dumps({"error": "Empty tool response"})


# Start background monitor thread
monitor_thread = threading.Thread(target=run_trade_monitor, daemon=True)
monitor_thread.start()


# ─────────────────────────────────────────────
#  AGENT LOOP
# ─────────────────────────────────────────────
def trim_history(history: list, max_pairs: int = 6) -> list:
    """Keep only the last N user/assistant pairs to reduce token overhead."""
    # Always keep tool results intact — only trim at message boundaries
    if len(history) <= max_pairs * 2:
        return history
    return history[-(max_pairs * 2):]


def run_agent(conversation_history: list) -> str:
    """Non-streaming agent — used internally."""
    messages = trim_history(conversation_history.copy())
    current_time = datetime.now().strftime("%Y-%m-%d %I:%M %p ET")
    system_with_time = SYSTEM_PROMPT + f"\n\n## CURRENT TIME\nThe current date and time is {current_time}. Always use this as your reference — never estimate or guess the time."
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system_with_time,
            tools=TOOLS,
            messages=messages
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            return "\n".join(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results = [None] * len(tool_blocks)
            def _run(idx_block):
                idx, block = idx_block
                print(f"[FinSight] Tool: {block.name} | Input: {block.input}")
                result = run_tool(block.name, block.input)
                return idx, {"type": "tool_result", "tool_use_id": block.id,
                             "content": result or json.dumps({"error": "Empty response"})}
            with ThreadPoolExecutor(max_workers=min(len(tool_blocks), 6)) as ex:
                for idx, tr in ex.map(_run, enumerate(tool_blocks)):
                    tool_results[idx] = tr
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
        else:
            return "\n".join(b.text for b in response.content if hasattr(b, "text")) or "Unexpected error."


def run_agent_streaming(conversation_history: list):
    """
    Streaming agent — runs tool calls in parallel then streams final response.
    Yields SSE chunks: data: {"text": "..."} or data: [DONE]
    """
    import json as _json
    messages = trim_history(conversation_history.copy())
    current_time = datetime.now().strftime("%Y-%m-%d %I:%M %p ET")
    system_with_time = SYSTEM_PROMPT + f"\n\n## CURRENT TIME\nThe current date and time is {current_time}. Always use this as your reference — never estimate or guess the time."

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system_with_time,
            tools=TOOLS,
            messages=messages
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Final text — stream it
            final_text = "\n".join(b.text for b in response.content if hasattr(b, "text"))
            # Stream word by word for smooth feel
            words = final_text.split(" ")
            chunk = ""
            for i, word in enumerate(words):
                chunk += word + (" " if i < len(words) - 1 else "")
                if len(chunk) >= 4:  # emit every ~4 chars
                    yield f"data: {_json.dumps({'text': chunk})}\n\n"
                    chunk = ""
            if chunk:
                yield f"data: {_json.dumps({'text': chunk})}\n\n"
            yield "data: [DONE]\n\n"
            return

        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results = [None] * len(tool_blocks)
            def _run_s(idx_block):
                idx, block = idx_block
                print(f"[FinSight] Tool: {block.name} | Input: {block.input}")
                result = run_tool(block.name, block.input)
                return idx, {"type": "tool_result", "tool_use_id": block.id,
                             "content": result or _json.dumps({"error": "Empty response"})}
            with ThreadPoolExecutor(max_workers=min(len(tool_blocks), 6)) as ex:
                for idx, tr in ex.map(_run_s, enumerate(tool_blocks)):
                    tool_results[idx] = tr
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
        else:
            final_text = "\n".join(b.text for b in response.content if hasattr(b, "text")) or "Unexpected error."
            yield f"data: {_json.dumps({'text': final_text})}\n\n"
            yield "data: [DONE]\n\n"
            return


# ─────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password     = request.form.get("password", "")
        app_password = os.getenv("APP_PASSWORD", "")
        if app_password and hash_password(password) == hash_password(app_password):
            session["authenticated"] = True
            session.permanent = False
            return redirect(url_for("index"))
        error = "Invalid password."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
@login_required
def chat():
    import json as _json
    data         = request.json
    history      = data.get("history", [])
    user_message = data.get("message", "")
    images       = data.get("images", [])  # [{base64, media_type}]

    if not user_message and not images:
        return jsonify({"error": "No message provided"}), 400

    # Build user content — text + optional images
    if images:
        user_content = []
        for img in images:
            user_content.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": img.get("media_type", "image/jpeg"),
                    "data":       img.get("base64", "")
                }
            })
        user_content.append({
            "type": "text",
            "text": user_message or "Analyze this image and give me your trading take."
        })
    else:
        user_content = user_message

    history.append({"role": "user", "content": user_content})

    def generate():
        try:
            yield from run_agent_streaming(history)
        except Exception as e:
            yield f"data: {_json.dumps({'text': f'Error: {str(e)}'})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*"
        }
    )


@app.route("/monitor")
@login_required
def monitor_status():
    with monitor_lock:
        return jsonify({
            "active_monitors": {
                k: {**v, "entry_time": v["entry_time"].isoformat(),
                    "elapsed_min": round((datetime.now()-v["entry_time"]).total_seconds()/60, 1)}
                for k, v in trade_monitors.items()
            },
            "recent_log": list(monitor_log[-20:])
        })


@app.route("/trades")
@login_required
def trades_dashboard():
    trades  = db_get_all_trades()
    summary = db_get_performance_summary()
    return render_template("trades.html", trades=trades, summary=summary)


@app.route("/api/trades")
@login_required
def api_trades():
    return jsonify({"trades": db_get_all_trades(), "summary": db_get_performance_summary()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"FinSight running at http://0.0.0.0:{port}")
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
