"""
Prediction market engine: Kalshi, Polymarket, Bayesian probability, Kelly Criterion.
"""
import math
import json as _json
import requests
from datetime import datetime
from core.config import KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY, KALSHI_BASE_URL, POLYMARKET_BASE_URL


# ── Kalshi ───────────────────────────────────────────────────

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
            key_data.encode(), password=None, backend=default_backend())
        signature  = private_key.sign(msg_string.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        sig_b64    = base64.b64encode(signature).decode("utf-8")
        return {
            "KALSHI-ACCESS-KEY":       KALSHI_API_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "Content-Type":            "application/json",
            "Accept":                  "application/json",
        }
    except Exception:
        return {
            "Authorization": f"Bearer {KALSHI_API_KEY_ID}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }


def get_kalshi_markets(query: str = "", limit: int = 10, status: str = "open") -> dict:
    try:
        params = {"limit": limit, "status": status}
        if query:
            params["search"] = query
        path = "/trade-api/v2/markets"
        r = requests.get(f"{KALSHI_BASE_URL}/markets",
                         headers=kalshi_headers("GET", path), params=params, timeout=10)
        markets = r.json().get("markets", [])
        results = []
        for m in markets:
            yes_price    = m.get("yes_ask") or m.get("yes_bid") or 0
            no_price     = m.get("no_ask")  or m.get("no_bid")  or 0
            implied_prob = round(yes_price / 100, 4) if yes_price else None
            results.append({
                "ticker": m.get("ticker"), "title": m.get("title"),
                "category": m.get("category"), "status": m.get("status"),
                "close_time": m.get("close_time"),
                "yes_ask": yes_price, "no_ask": no_price,
                "implied_prob": implied_prob, "volume": m.get("volume"),
                "open_interest": m.get("open_interest"),
                "rules": (m.get("rules_primary", "") or "")[:200],
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
            "ticker": m.get("ticker"), "title": m.get("title"),
            "category": m.get("category"), "status": m.get("status"),
            "close_time": m.get("close_time"),
            "yes_ask": yes_price, "no_ask": no_price,
            "implied_prob": implied_prob, "volume_24h": m.get("volume_24h"),
            "open_interest": m.get("open_interest"),
            "rules": (m.get("rules_primary", "") or "")[:500],
            "source": "Kalshi",
        }
    except Exception as e:
        return {"error": str(e)}


def get_prediction_market_categories() -> dict:
    try:
        path = "/trade-api/v2/markets"
        r = requests.get(f"{KALSHI_BASE_URL}/markets",
                         headers=kalshi_headers("GET", path),
                         params={"limit": 100, "status": "open"}, timeout=10)
        markets    = r.json().get("markets", [])
        categories: dict = {}
        for m in markets:
            cat = m.get("category", "Other")
            categories[cat] = categories.get(cat, 0) + 1
        return {
            "categories": [{"category": k, "market_count": v}
                           for k, v in sorted(categories.items(), key=lambda x: -x[1])],
            "total_open_markets": len(markets),
            "source": "Kalshi",
        }
    except Exception as e:
        return {"error": str(e)}


# ── Polymarket ───────────────────────────────────────────────

def get_polymarket_markets(query: str = "", limit: int = 10, sort_by_ending: bool = False) -> dict:
    try:
        from datetime import timezone
        params = {"limit": 100, "active": "true", "closed": "false"}
        if query:
            params["search"] = query
        r   = requests.get(f"{POLYMARKET_BASE_URL}/markets", params=params, timeout=10)
        raw = r.json()
        markets = raw if isinstance(raw, list) else raw.get("markets", [])

        def parse_prices(m):
            prices_str = m.get("outcomePrices", "[]")
            prices = _json.loads(prices_str) if isinstance(prices_str, str) else (prices_str or [])
            try:
                if prices and len(prices) >= 2:
                    return round(float(prices[0]) * 100, 1), round(float(prices[1]) * 100, 1)
            except Exception:
                pass
            return None, None

        results = []
        for m in markets:
            yes_price, no_price = parse_prices(m)
            if yes_price is None or yes_price == 0 or yes_price == 100:
                continue
            liq = float(m.get("liquidity") or 0)
            if liq < 100:
                continue
            results.append({
                "id": m.get("id"), "question": m.get("question"),
                "category": m.get("category", ""), "end_date": m.get("endDate", ""),
                "yes_price": yes_price, "no_price": no_price,
                "implied_prob": round(yes_price / 100, 4),
                "volume": m.get("volume"), "liquidity": liq,
                "url": f"https://polymarket.com/event/{m.get('slug', '')}",
            })

        time_keywords = ["tonight", "today", "live", "game", "nba", "nhl", "mlb", "nfl", "match"]
        if sort_by_ending or any(k in query.lower() for k in time_keywords):
            def end_sort(m):
                ed = m.get("end_date", "")
                try:
                    return datetime.fromisoformat(ed.replace("Z", "+00:00"))
                except Exception:
                    return datetime(2099, 1, 1, tzinfo=timezone.utc)
            results.sort(key=end_sort)

        return {
            "markets": results[:limit], "count": len(results[:limit]),
            "source": "Polymarket (liquid markets only, sorted by end date)",
        }
    except Exception as e:
        return {"error": str(e)}


def get_polymarket_market_detail(market_id: str) -> dict:
    try:
        r = requests.get(f"{POLYMARKET_BASE_URL}/markets/{market_id}", timeout=10)
        m = r.json()
        outcomes   = m.get("outcomes", "[]")
        prices_str = m.get("outcomePrices", "[]")
        if isinstance(prices_str, str):
            try:
                prices = _json.loads(prices_str)
            except Exception:
                prices = []
        else:
            prices = prices_str or []

        yes_price = no_price = None
        if prices and len(prices) >= 2:
            try:
                yes_price = round(float(prices[0]) * 100, 1)
                no_price  = round(float(prices[1]) * 100, 1)
            except Exception:
                pass

        return {
            "id": m.get("id"), "question": m.get("question"),
            "description": (m.get("description") or "")[:400],
            "category": m.get("category", ""), "end_date": m.get("endDate", ""),
            "yes_price": yes_price, "no_price": no_price,
            "implied_prob": round(yes_price / 100, 4) if yes_price else None,
            "volume": m.get("volume"), "liquidity": m.get("liquidity"),
            "url": f"https://polymarket.com/event/{m.get('slug', '')}",
            "source": "Polymarket",
        }
    except Exception as e:
        return {"error": str(e)}


def search_prediction_markets(query: str, limit: int = 8) -> dict:
    """Search both Kalshi and Polymarket simultaneously."""
    kalshi_results = get_kalshi_markets(query, limit=limit // 2)
    poly_results   = get_polymarket_markets(query, limit=limit // 2)

    kalshi_markets = kalshi_results.get("markets", []) if "error" not in kalshi_results else []
    poly_markets   = poly_results.get("markets", [])   if "error" not in poly_results   else []

    for m in kalshi_markets: m["source"] = "Kalshi"
    for m in poly_markets:   m["source"] = "Polymarket"

    all_markets = kalshi_markets + poly_markets
    priced      = [m for m in all_markets if m.get("implied_prob") is not None]
    unpriced    = [m for m in all_markets if m.get("implied_prob") is None]

    return {
        "query": query,
        "priced_markets": priced,
        "unpriced_markets_count": len(unpriced),
        "kalshi_count": len(kalshi_markets),
        "poly_count": len(poly_markets),
        "note": "Priced markets only are recommended for betting — unpriced have no liquidity.",
    }


# ── Math ─────────────────────────────────────────────────────

def calculate_bayesian_probability(base_rate: float, evidence_items: list) -> dict:
    try:
        prior_odds    = base_rate / (1 - base_rate) if base_rate < 1 else 999
        posterior_odds = prior_odds
        steps = [{"step": "Prior", "odds": round(prior_odds, 4), "prob": round(base_rate, 4),
                  "note": f"Base rate: {base_rate*100:.1f}%"}]

        for item in evidence_items:
            lr = item.get("likelihood_ratio", 1.0)
            posterior_odds *= lr
            prob = posterior_odds / (1 + posterior_odds)
            steps.append({
                "step": item.get("description", "Evidence"), "lr": lr,
                "odds": round(posterior_odds, 4), "prob": round(prob, 4),
                "direction": "supports YES" if lr > 1 else "supports NO" if lr < 1 else "neutral",
            })

        final_prob = posterior_odds / (1 + posterior_odds)
        return {
            "prior": base_rate, "posterior": round(final_prob, 4),
            "posterior_pct": f"{final_prob*100:.1f}%",
            "reasoning_chain": steps,
            "confidence": ("high" if len(evidence_items) >= 3 else
                           "medium" if len(evidence_items) >= 1 else "low"),
        }
    except Exception as e:
        return {"error": str(e)}


def calculate_kelly_criterion(our_probability: float, market_yes_price: float,
                              bankroll: float = 1000.0, max_fraction: float = 0.25) -> dict:
    try:
        p  = our_probability
        q  = 1 - p
        b  = (100 - market_yes_price) / market_yes_price
        kf = max(0, min((p * b - q) / b, max_fraction))
        bet_amount = round(bankroll * kf, 2)
        edge       = round((p - (market_yes_price / 100)) * 100, 2)
        return {
            "our_prob": f"{p*100:.1f}%", "market_prob": f"{market_yes_price:.0f}%",
            "edge": f"{edge:+.1f}%", "kelly_fraction": f"{kf*100:.1f}%",
            "recommended_bet": f"${bet_amount:.2f}", "bankroll": f"${bankroll:.2f}",
            "bet_side": "YES" if p > market_yes_price / 100 else "NO",
            "signal": ("strong edge" if abs(edge) > 10 else
                       "moderate edge" if abs(edge) > 5 else "weak edge — consider passing"),
            "note": "Kelly fraction capped at 25% max to prevent overbetting",
        }
    except Exception as e:
        return {"error": str(e)}


def analyze_prediction_market(ticker: str, our_probability: float = None) -> dict:
    try:
        detail = get_kalshi_market_detail(ticker)
        if "error" in detail:
            return detail
        yes_price    = detail.get("yes_ask", 0)
        implied_prob = detail.get("implied_prob", 0)
        result = {"market": detail, "implied_prob": f"{implied_prob*100:.1f}%" if implied_prob else "N/A"}
        if our_probability is not None:
            edge = round((our_probability - implied_prob) * 100, 2)
            result["our_probability"] = f"{our_probability*100:.1f}%"
            result["edge"]            = f"{edge:+.1f}%"
            result["bet_side"]        = "YES" if our_probability > implied_prob else "NO"
            result["signal"]          = (
                "strong edge — consider betting" if abs(edge) > 10 else
                "moderate edge" if abs(edge) > 5 else "weak edge — market may be efficient here")
            result["kelly_sizing"] = calculate_kelly_criterion(our_probability, yes_price)
        return result
    except Exception as e:
        return {"error": str(e)}
