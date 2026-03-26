"""
Technical analysis: trading session quality, VWAP, expected move, 0DTE context.
"""
import os
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.config import ALPACA_DATA_URL, ALPACA_HEADERS, POLYGON_API_KEY
from core.market_data import cache_get, cache_set, get_stock_price


def get_trading_session() -> dict:
    now       = datetime.now()
    total_min = now.hour * 60 + now.minute

    OPEN        = 9  * 60 + 30
    CLOSE       = 16 * 60
    C_OPEN_END  = 9  * 60 + 45
    BEST_START  = 10 * 60
    BEST_END    = 11 * 60 + 30
    LUNCH_END   = 14 * 60
    POWER_START = 15 * 60
    CLOSE_WARN  = 15 * 60 + 30

    if total_min < OPEN or total_min >= CLOSE:
        return {"session": "closed",      "quality": "none", "recommendation": "avoid",
                "warning": "Market is closed. No 0DTE trades."}
    if total_min < C_OPEN_END:
        return {"session": "open_chop",   "quality": "poor", "recommendation": "avoid",
                "warning": "First 15 min after open — high volatility, fakeouts common."}
    if total_min < BEST_START:
        return {"session": "early",       "quality": "fair", "recommendation": "caution",
                "warning": "9:45-10:00 window. OK but not ideal — direction still setting."}
    if total_min < BEST_END:
        return {"session": "prime",       "quality": "best", "recommendation": "favorable — best 0DTE window",
                "warning": None}
    if total_min < LUNCH_END:
        return {"session": "lunch_chop",  "quality": "poor", "recommendation": "avoid",
                "warning": "11:30-2:00 lunch chop. Low volume, choppy price action."}
    if total_min < POWER_START:
        return {"session": "afternoon",   "quality": "fair", "recommendation": "caution",
                "warning": "2:00-3:00 afternoon window. Directional moves possible."}
    if total_min < CLOSE_WARN:
        return {"session": "power_hour",  "quality": "good", "recommendation": "high conviction only",
                "warning": "Power hour. High conviction plays only — theta burning fast."}
    return     {"session": "late",        "quality": "poor", "recommendation": "avoid",
                "warning": "Under 30 min to close. Theta decay extreme. 0DTE extremely risky."}


def calculate_vwap(ticker: str) -> dict:
    ticker = ticker.upper()
    cached = cache_get(f"vwap_{ticker}")
    if cached:
        return cached
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{today}/{today}",
            params={"adjusted": "true", "sort": "asc", "limit": 500, "apiKey": POLYGON_API_KEY},
            timeout=10)
        bars = r.json().get("results", [])
        if not bars:
            return {"error": "No intraday data for VWAP calculation"}

        cum_pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars)
        cum_v  = sum(b["v"] for b in bars)
        if cum_v == 0:
            return {"error": "Zero volume — cannot compute VWAP"}

        vwap        = round(cum_pv / cum_v, 4)
        current     = bars[-1]["c"]
        pct_vs_vwap = round(((current - vwap) / vwap) * 100, 3)
        position    = "above" if current > vwap else "below"

        result = {
            "ticker": ticker, "vwap": vwap, "current": current,
            "pct_vs_vwap": pct_vs_vwap,
            "position": f"Price is {position} VWAP by {abs(pct_vs_vwap)}%",
            "bars_used": len(bars), "source": "Polygon.io (intraday 1-min bars)",
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
            headers=headers, params={"symbol": ticker, "includeAllRoots": "true"}, timeout=10)
        expirations = exp_r.json().get("expirations", {}).get("date", [])
        expiry = next((e for e in expirations if e >= today), None)
        if not expiry:
            return {"error": "No valid expiration found"}

        chain_r = requests.get(
            "https://api.tradier.com/v1/markets/options/chains",
            headers=headers,
            params={"symbol": ticker, "expiration": expiry, "greeks": "true"}, timeout=10)
        options = chain_r.json().get("options", {}).get("option", [])

        snap  = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/snapshot",
                             headers=ALPACA_HEADERS, timeout=8).json()
        price = snap.get("latestTrade", {}).get("p") or snap.get("dailyBar", {}).get("c") or 0

        atm = min(options, key=lambda o: abs(o.get("strike", 0) - price), default=None)
        if not atm or not atm.get("greeks"):
            return {"error": "Could not find ATM option for expected move"}

        iv            = atm["greeks"].get("mid_iv") or 0
        dte           = 1 / 365
        expected_move = round(price * iv * (dte ** 0.5), 2)
        em_pct        = round((expected_move / price) * 100, 2) if price else 0
        daily_high    = snap.get("dailyBar", {}).get("h") or price
        daily_low     = snap.get("dailyBar", {}).get("l") or price
        already_moved = round(daily_high - daily_low, 2)
        am_pct        = round((already_moved / price) * 100, 2) if price else 0
        pct_used      = round((already_moved / expected_move) * 100, 1) if expected_move else 0

        return {
            "ticker": ticker, "current_price": price, "atm_iv": round(iv * 100, 2),
            "expiry": expiry, "expected_move": f"±${expected_move}", "expected_move_pct": f"±{em_pct}%",
            "upper_bound": round(price + expected_move, 2), "lower_bound": round(price - expected_move, 2),
            "already_moved": f"${already_moved} ({am_pct}%)",
            "pct_of_move_used": f"{pct_used}% of expected daily range used",
            "signal": ("late — most of the move is done" if pct_used > 70 else
                       "room to move" if pct_used < 40 else "moderate — proceed with caution"),
            "source": "Tradier (IV) + Alpaca (price)",
        }
    except Exception as e:
        return {"error": str(e)}


def get_0dte_context(ticker: str, option_type: str = "call") -> dict:
    """
    Runs session check, VWAP, expected move, and stock price in parallel.
    Replaces 4 sequential tool calls with 1 parallel call.
    """
    ticker = ticker.upper()

    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            ex.submit(get_trading_session):               "session",
            ex.submit(calculate_vwap, ticker):            "vwap",
            ex.submit(calculate_expected_move, ticker, option_type): "expected_move",
            ex.submit(get_stock_price, ticker):           "price",
        }
        for f in as_completed(futures):
            key = futures[f]
            try:
                results[key] = f.result()
            except Exception as e:
                results[key] = {"error": str(e)}

    session  = results.get("session", {})
    vwap     = results.get("vwap", {})
    em       = results.get("expected_move", {})
    price    = results.get("price", {})
    quality  = session.get("quality", "unknown")
    tradeable = quality in ("prime", "good", "fair")

    return {
        "ticker":          ticker,
        "current_price":   price.get("price", "N/A"),
        "session_quality": quality,
        "session_window":  session.get("session", "unknown"),
        "session_warning": session.get("warning", ""),
        "recommendation":  session.get("recommendation", "unknown"),
        "tradeable":       tradeable,
        "vwap_position":   vwap.get("position", "N/A"),
        "vwap":            vwap.get("vwap"),
        "expected_move":   em.get("expected_move", "N/A"),
        "move_used":       em.get("pct_of_move_used", "N/A"),
        "move_signal":     em.get("signal", "N/A"),
        "atm_iv":          em.get("atm_iv"),
        "upper_bound":     em.get("upper_bound"),
        "lower_bound":     em.get("lower_bound"),
        "note": ("All data fetched in parallel. Use get_options_chain next for strikes."
                 if tradeable else f"NOT recommended: {session.get('warning','')}"),
        "source": "Parallel fetch: Alpaca + Polygon + Tradier",
    }
