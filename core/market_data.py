"""
Market data fetching: stocks, crypto, indices, technicals, news.
"""
import time
import threading
import requests
from datetime import datetime, timedelta
from core.config import (
    ALPACA_DATA_URL, ALPACA_CRYPTO_URL, ALPACA_HEADERS,
    CRYPTO_SYMBOLS, POLYGON_API_KEY, NEWS_API_KEY
)

# ── 60-second TTL cache ──────────────────────────────────────
_cache: dict = {}
_cache_lock  = threading.Lock()


def cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["ts"]) < 60:
            return entry["val"]
    return None


def cache_set(key, val):
    with _cache_lock:
        _cache[key] = {"val": val, "ts": time.time()}


# ── Stock / crypto prices ────────────────────────────────────

def get_crypto_price(symbol: str) -> dict:
    symbol = symbol.upper().replace("/USD", "").replace("USD", "")
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
            "asset_type": "crypto", "source": "Alpaca Crypto (real-time)",
        }
    except Exception as e:
        return {"error": str(e)}


def get_stock_price(ticker: str) -> dict:
    ticker = ticker.upper().replace("/USD", "").replace("USD", "")
    if ticker in CRYPTO_SYMBOLS:
        return get_crypto_price(ticker)
    try:
        trade_r = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/trades/latest",
                               headers=ALPACA_HEADERS, timeout=10)
        quote_r = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/quotes/latest",
                               headers=ALPACA_HEADERS, timeout=10)
        snap_r  = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/snapshot",
                               headers=ALPACA_HEADERS, timeout=10)
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
            "asset_type": "stock", "source": "Alpaca Markets (real-time)",
        }
    except Exception as e:
        return {"error": str(e)}


# ── Market overview ──────────────────────────────────────────

def get_market_overview() -> dict:
    cached = cache_get("market_overview")
    if cached:
        return cached
    results = {}
    for ticker in ["SPY", "QQQ", "DIA", "IWM"]:
        try:
            snap  = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/snapshot",
                                 headers=ALPACA_HEADERS, timeout=10).json()
            daily = snap.get("dailyBar", {})
            prev  = snap.get("prevDailyBar", {})
            trade = snap.get("latestTrade", {})
            lp    = trade.get("p") or daily.get("c")
            pc    = prev.get("c")
            results[ticker] = {
                "price": lp, "open": daily.get("o"), "high": daily.get("h"), "low": daily.get("l"),
                "prev_close": pc,
                "change_pct": round(((lp - pc) / pc) * 100, 2) if lp and pc else None,
                "volume": daily.get("v"),
            }
        except Exception as e:
            results[ticker] = {"error": str(e)}
    try:
        vix = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/VIX/prev?adjusted=true&apiKey={POLYGON_API_KEY}",
            timeout=10).json()
        if vix.get("resultsCount", 0) > 0:
            v = vix["results"][0]
            results["VIX"] = {"close": v["c"], "open": v["o"], "note": "Prior day close via Polygon"}
    except Exception:
        results["VIX"] = {"error": "Failed to fetch VIX"}
    result = {
        "indices": results,
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "Alpaca (real-time) + Polygon (VIX)",
    }
    cache_set("market_overview", result)
    return result


def fetch_market_snapshot() -> dict:
    """Full market snapshot used by the autonomous scheduler: indices, VIX, crypto, news."""
    snapshot = {}

    # Indices
    indices = {}
    for t in ["SPY", "QQQ", "IWM", "DIA"]:
        try:
            s     = requests.get(f"{ALPACA_DATA_URL}/stocks/{t}/snapshot",
                                 headers=ALPACA_HEADERS, timeout=10).json()
            daily = s.get("dailyBar", {})
            prev  = s.get("prevDailyBar", {})
            trade = s.get("latestTrade", {})
            lp    = trade.get("p") or daily.get("c")
            pc    = prev.get("c")
            indices[t] = {
                "price": lp, "open": daily.get("o"),
                "high": daily.get("h"), "low": daily.get("l"),
                "change_pct": round(((lp - pc) / pc) * 100, 2) if lp and pc else None,
                "volume": daily.get("v"),
            }
        except Exception as e:
            indices[t] = {"error": str(e)}
    snapshot["indices"] = indices

    # VIX
    try:
        vr = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/VIX/prev?adjusted=true&apiKey={POLYGON_API_KEY}",
            timeout=10).json()
        if vr.get("resultsCount", 0) > 0:
            snapshot["VIX"] = vr["results"][0]["c"]
    except Exception:
        snapshot["VIX"] = "unavailable"

    # Crypto
    crypto_prices = {}
    for sym in ["BTC", "ETH", "SOL", "DOGE"]:
        try:
            pair = f"{sym}/USD"
            sr   = requests.get(f"{ALPACA_CRYPTO_URL}/snapshots", headers=ALPACA_HEADERS,
                                params={"symbols": pair}, timeout=8).json()
            s    = sr.get("snapshots", {}).get(pair, {})
            t    = s.get("latestTrade", {})
            d    = s.get("dailyBar", {})
            p    = s.get("prevDailyBar", {})
            lp   = t.get("p") or d.get("c")
            pc   = p.get("c")
            crypto_prices[sym] = {
                "price": lp,
                "change_pct": round(((lp - pc) / pc) * 100, 2) if lp and pc else None,
                "volume": d.get("v"),
            }
        except Exception as e:
            crypto_prices[sym] = {"error": str(e)}
    snapshot["crypto"] = crypto_prices

    # News
    try:
        nr = requests.get("https://newsapi.org/v2/top-headlines", params={
            "category": "business", "language": "en",
            "pageSize": 5, "apiKey": NEWS_API_KEY,
        }, timeout=10).json()
        snapshot["top_news"] = [
            {"title": a["title"], "source": a["source"]["name"], "published": a["publishedAt"]}
            for a in nr.get("articles", [])[:5]
        ]
    except Exception:
        snapshot["top_news"] = []

    snapshot["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S ET")
    return snapshot


# ── Technicals ───────────────────────────────────────────────

def get_stock_technicals(ticker: str, days: int = 30) -> dict:
    ticker     = ticker.upper()
    end_date   = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}"
            f"?adjusted=true&sort=asc&apiKey={POLYGON_API_KEY}", timeout=10)
        results = r.json().get("results", [])
        if not results:
            return {"error": f"No historical data for {ticker}"}
        closes = [d["c"] for d in results]
        highs  = [d["h"] for d in results]
        lows   = [d["l"] for d in results]

        def sma(p, n):
            return round(sum(p[-n:]) / n, 2) if len(p) >= n else None

        def rsi(c, n=14):
            if len(c) < n + 1:
                return None
            gains  = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
            losses = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
            ag, al = sum(gains[-n:]) / n, sum(losses[-n:]) / n
            return 100 if al == 0 else round(100 - (100 / (1 + ag / al)), 2)

        return {
            "ticker": ticker, "period_days": days, "current_price": closes[-1],
            "52w_high": round(max(highs), 2), "52w_low": round(min(lows), 2),
            "sma_10": sma(closes, 10), "sma_20": sma(closes, 20), "sma_50": sma(closes, 50),
            "rsi_14": rsi(closes),
            "support": round(min(lows[-10:]), 2), "resistance": round(max(highs[-10:]), 2),
            "recent_5_days": [
                {"date": datetime.fromtimestamp(d["t"] / 1000).strftime("%Y-%m-%d"),
                 "open": d["o"], "high": d["h"], "low": d["l"], "close": d["c"]}
                for d in results[-5:]
            ],
            "source": "Polygon.io (historical)",
        }
    except Exception as e:
        return {"error": str(e)}


def get_financial_news(query: str, num_articles: int = 3) -> dict:
    try:
        r = requests.get("https://newsapi.org/v2/everything", params={
            "q": query, "language": "en", "sortBy": "publishedAt",
            "pageSize": min(num_articles, 5), "apiKey": NEWS_API_KEY,
        }, timeout=10)
        articles = r.json().get("articles", [])
        if not articles:
            return {"error": "No articles found."}
        return {
            "query": query,
            "articles": [
                {"title": a["title"], "source": a["source"]["name"],
                 "published": a["publishedAt"], "summary": a.get("description", ""), "url": a["url"]}
                for a in articles
            ],
            "source": "NewsAPI.org",
        }
    except Exception as e:
        return {"error": str(e)}
