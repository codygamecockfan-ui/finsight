"""
Implementations for all Claude tools callable from the agent loop.
Includes NBA sports data, Kalshi trading, and Polymarket — in addition to 
the core modules already in core/.
"""
import os
import json
import uuid
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from core.config import (
    ALPACA_TRADING_URL, ALPACA_DATA_URL, ALPACA_HEADERS,
    CRYPTO_SYMBOLS, MAX_OPTIONS_PREMIUM,
    KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY, KALSHI_BASE_URL,
)
from core.database import (
    db_log_entry, db_log_exit, db_get_all_trades, db_get_performance_summary,
    db_set_monitor, db_remove_monitor, db_has_open_position
)
from core.market_data import (
    get_stock_price, get_stock_technicals, get_market_overview, get_financial_news
)
from core.analysis import (
    get_trading_session, calculate_vwap, calculate_expected_move, get_0dte_context
)
from core.prediction import (
    get_kalshi_markets, get_kalshi_market_detail, analyze_prediction_market,
    calculate_bayesian_probability, calculate_kelly_criterion, get_prediction_market_categories,
    get_polymarket_markets, get_polymarket_market_detail, search_prediction_markets
)
from core.monitor import trade_monitors, monitor_lock, monitor_log


# BallDontLie API (NBA sports data)
BALLDONTLIE_API_KEY  = os.getenv("BALLDONTLIE_API_KEY", "")
BALLDONTLIE_BASE_URL = "https://api.balldontlie.io"


# ─────────────────────────────────────────────
#  KALSHI SIGNED REQUESTS (RSA-PSS)
# ─────────────────────────────────────────────
def _kalshi_sign(method: str, path: str) -> dict:
    """Generate Kalshi RSA-PSS signed headers."""
    import base64
    import time as _time
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as _padding
    from cryptography.hazmat.backends import default_backend

    ts        = str(int(_time.time() * 1000))
    sign_path = path.split("?")[0]
    message   = f"{ts}{method.upper()}{sign_path}".encode("utf-8")
    key_data  = KALSHI_PRIVATE_KEY.replace("\\n", "\n")

    pk = serialization.load_pem_private_key(
        key_data.encode(), password=None, backend=default_backend()
    )
    sig = pk.sign(
        message,
        _padding.PSS(mgf=_padding.MGF1(hashes.SHA256()), salt_length=_padding.PSS.DIGEST_LENGTH),
        hashes.SHA256()
    )
    return {
        "KALSHI-ACCESS-KEY":       KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
        "Content-Type":            "application/json",
        "Accept":                  "application/json"
    }


# ─────────────────────────────────────────────
#  KALSHI TRADING (real money)
# ─────────────────────────────────────────────
def get_kalshi_balance() -> dict:
    try:
        path = "/trade-api/v2/portfolio/balance"
        hdrs = _kalshi_sign("GET", path)
        r    = requests.get(f"{KALSHI_BASE_URL}/portfolio/balance", headers=hdrs, timeout=10)
        d    = r.json()
        bal_cents = d.get("balance", 0)
        return {"available_balance": f"${int(bal_cents) / 100:.2f}", "source": "Kalshi"}
    except Exception as e:
        return {"error": str(e)}


def get_kalshi_positions() -> dict:
    try:
        path = "/trade-api/v2/portfolio/positions"
        hdrs = _kalshi_sign("GET", path)
        r    = requests.get(f"{KALSHI_BASE_URL}/portfolio/positions", headers=hdrs, timeout=10)
        positions = r.json().get("market_positions", [])
        results = []
        for p in positions:
            if p.get("position", 0) != 0:
                results.append({
                    "ticker":         p.get("market_id") or p.get("ticker"),
                    "position":       p.get("position"),
                    "yes_price":      p.get("market_exposure_cents", 0) / 100 if p.get("market_exposure_cents") else None,
                    "realized_pnl":   f"${p.get('realized_pnl_cents', 0) / 100:.2f}",
                    "resting_orders": p.get("resting_orders_count", 0)
                })
        return {"positions": results, "count": len(results), "source": "Kalshi"}
    except Exception as e:
        return {"error": str(e)}


def get_kalshi_orders() -> dict:
    try:
        path = "/trade-api/v2/portfolio/orders"
        hdrs = _kalshi_sign("GET", path)
        r = requests.get(f"{KALSHI_BASE_URL}/portfolio/orders",
                         headers=hdrs, params={"status": "resting"}, timeout=10)
        orders = r.json().get("orders", [])
        return {
            "open_orders": [{
                "order_id": o.get("order_id"), "ticker": o.get("ticker"),
                "side": o.get("side"), "action": o.get("action"),
                "count": o.get("count"),
                "yes_price": f"${o.get('yes_price', 0) / 100:.2f}",
                "status": o.get("status"),
            } for o in orders],
            "count": len(orders), "source": "Kalshi"
        }
    except Exception as e:
        return {"error": str(e)}


def place_kalshi_order(ticker: str, side: str, count: int, yes_price_cents: int,
                       action: str = "buy") -> dict:
    """Place a REAL Kalshi order. Max $5 per bet."""
    try:
        cost_dollars = (count * yes_price_cents) / 100
        if cost_dollars > 5.00:
            return {"error": f"Order exceeds $5.00 max bet limit. Cost: ${cost_dollars:.2f}."}
        if yes_price_cents < 1 or yes_price_cents > 99:
            return {"error": f"yes_price_cents must be 1-99. Got: {yes_price_cents}"}
        if count < 1:
            return {"error": "count must be at least 1"}

        path = "/trade-api/v2/portfolio/orders"
        hdrs = _kalshi_sign("POST", path)
        order_data = {
            "ticker":          ticker.upper(),
            "action":          action.lower(),
            "side":            side.lower(),
            "count":           count,
            "type":            "limit",
            "yes_price":       yes_price_cents,
            "client_order_id": str(uuid.uuid4())
        }
        r = requests.post(f"{KALSHI_BASE_URL}/portfolio/orders",
                          headers=hdrs, json=order_data, timeout=10)
        if r.status_code == 201:
            order = r.json().get("order", {})
            return {
                "status":       "ORDER PLACED",
                "order_id":     order.get("order_id"),
                "ticker":       ticker, "side": side, "action": action, "count": count,
                "yes_price":    f"${yes_price_cents / 100:.2f}",
                "total_cost":   f"${cost_dollars:.2f}",
                "order_status": order.get("status"),
                "source":       "Kalshi (LIVE ORDER)"
            }
        return {"error": f"Order failed: {r.status_code}", "details": r.json()}
    except Exception as e:
        return {"error": str(e)}


def cancel_kalshi_order(order_id: str) -> dict:
    try:
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        hdrs = _kalshi_sign("DELETE", path)
        r = requests.delete(f"{KALSHI_BASE_URL}/portfolio/orders/{order_id}",
                            headers=hdrs, timeout=10)
        if r.status_code in (200, 204):
            return {"status": "Order cancelled", "order_id": order_id}
        return {"error": f"Cancel failed: {r.status_code}", "details": r.json()}
    except Exception as e:
        return {"error": str(e)}


def get_kalshi_events(query: str = "", category: str = "", limit: int = 10) -> dict:
    """Browse Kalshi events with nested markets — more reliable than /markets for sports."""
    try:
        params = {"limit": limit, "status": "open", "with_nested_markets": "true"}
        if query:    params["search"]   = query
        if category: params["category"] = category

        path = "/trade-api/v2/events"
        hdrs = _kalshi_sign("GET", path)
        r    = requests.get(f"{KALSHI_BASE_URL}/events",
                            headers=hdrs, params=params, timeout=10)
        all_events = r.json().get("events", [])

        results = []
        for e in all_events[:limit]:
            market_summaries = []
            for m in e.get("markets", []):
                yes_price = m.get("yes_ask") or m.get("yes_bid") or m.get("last_price") or 0
                no_price  = m.get("no_ask")  or m.get("no_bid")  or 0
                if yes_price == 0 and no_price > 0: yes_price = 100 - no_price
                if no_price == 0 and yes_price > 0: no_price  = 100 - yes_price
                market_summaries.append({
                    "ticker":       m.get("ticker"),
                    "title":        m.get("title") or m.get("subtitle"),
                    "yes_ask":      yes_price,
                    "no_ask":       no_price,
                    "implied_prob": round(yes_price / 100, 4) if yes_price else None,
                    "volume":       m.get("volume"),
                    "status":       m.get("status"),
                })
            results.append({
                "event_ticker": e.get("event_ticker"),
                "title":        e.get("title"),
                "category":     e.get("category"),
                "series":       e.get("series_ticker"),
                "close_time":   e.get("close_time") or e.get("expected_expiration_time"),
                "markets":      market_summaries,
                "market_count": len(market_summaries),
            })
        return {"events": results, "count": len(results), "source": "Kalshi /events"}
    except Exception as e:
        return {"error": str(e)}


def get_kalshi_sports_today() -> dict:
    """Get all live/upcoming sports markets on Kalshi, parallel fetch across categories."""
    def fetch_sports(category):
        try:
            path   = "/trade-api/v2/events"
            hdrs   = _kalshi_sign("GET", path)
            params = {"limit": 50, "status": "open", "with_nested_markets": "true", "category": category}
            r = requests.get(f"{KALSHI_BASE_URL}/events", headers=hdrs, params=params, timeout=10)
            return r.json().get("events", [])
        except Exception:
            return []

    sport_categories = ["Basketball", "Baseball", "Hockey", "Sports", "NBA", "MLB", "NHL"]
    futures_map = {}
    with ThreadPoolExecutor(max_workers=7) as ex:
        for cat in sport_categories:
            futures_map[ex.submit(fetch_sports, cat)] = cat

    seen, all_events = set(), []
    for fut in futures_map:
        try:
            for e in fut.result():
                t = e.get("event_ticker")
                if t and t not in seen:
                    seen.add(t)
                    all_events.append(e)
        except Exception:
            pass

    live_markets = []
    for e in all_events:
        for m in e.get("markets", []):
            yes_p = m.get("yes_ask") or m.get("yes_bid") or m.get("last_price") or 0
            no_p  = m.get("no_ask")  or m.get("no_bid")  or 0
            if yes_p == 0 and no_p > 0: yes_p = 100 - no_p
            if no_p == 0 and yes_p > 0: no_p  = 100 - yes_p
            live_markets.append({
                "ticker":       m.get("ticker"),
                "event":        e.get("title"),
                "market_title": m.get("title") or m.get("subtitle", ""),
                "category":     e.get("category"),
                "yes_ask":      yes_p,
                "no_ask":       no_p,
                "implied_prob": round(yes_p / 100, 4) if yes_p else None,
                "volume":       m.get("volume"),
                "close_time":   m.get("close_time") or e.get("close_time"),
                "status":       m.get("status"),
            })
    live_markets.sort(key=lambda x: float(x.get("volume") or 0), reverse=True)
    return {
        "sports_markets": live_markets[:30],
        "total_found":    len(live_markets),
        "source":         "Kalshi /events sports (parallel fetch)"
    }


# ─────────────────────────────────────────────
#  NBA DATA (BallDontLie)
# ─────────────────────────────────────────────
def _bdl_headers() -> dict:
    return {"Authorization": BALLDONTLIE_API_KEY}


def get_live_nba_games() -> dict:
    """NBA games today with live scores, quarter, time remaining."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        r = requests.get(
            f"{BALLDONTLIE_BASE_URL}/nba/v1/games",
            headers=_bdl_headers(),
            params={"dates[]": today, "per_page": 30},
            timeout=10
        )
        games = r.json().get("data", [])
        results = []
        for g in games:
            home = g.get("home_team", {})
            away = g.get("visitor_team", {})
            period = g.get("period", 0)
            results.append({
                "game_id":        g.get("id"),
                "status":         g.get("status", ""),
                "period":         f"Q{period}" if period and period <= 4 else ("OT" if period and period > 4 else "Pre"),
                "time_remaining": g.get("time", ""),
                "home_team":      f"{home.get('city')} {home.get('name')}",
                "home_abbr":      home.get("abbreviation"),
                "home_score":     g.get("home_team_score", 0),
                "visitor_team":   f"{away.get('city')} {away.get('name')}",
                "visitor_abbr":   away.get("abbreviation"),
                "visitor_score":  g.get("visitor_team_score", 0),
                "postseason":     g.get("postseason", False),
                "home_q1": g.get("home_q1"), "home_q2": g.get("home_q2"),
                "home_q3": g.get("home_q3"), "home_q4": g.get("home_q4"),
                "visitor_q1": g.get("visitor_q1"), "visitor_q2": g.get("visitor_q2"),
                "visitor_q3": g.get("visitor_q3"), "visitor_q4": g.get("visitor_q4"),
            })
        live     = [g for g in results if g["status"] not in ("Final", "") and ":" in str(g["time_remaining"])]
        final    = [g for g in results if g["status"] == "Final"]
        upcoming = [g for g in results if g["status"] not in ("Final",) and ":" not in str(g["time_remaining"])]
        return {
            "date": today, "live": live, "final": final, "upcoming": upcoming,
            "total": len(results), "source": "BallDontLie (real-time)"
        }
    except Exception as e:
        return {"error": str(e)}


def get_nba_injuries() -> dict:
    try:
        r = requests.get(
            f"{BALLDONTLIE_BASE_URL}/nba/v1/player_injuries",
            headers=_bdl_headers(), params={"per_page": 50}, timeout=10
        )
        injuries = r.json().get("data", [])
        results = []
        for inj in injuries:
            player = inj.get("player", {})
            team   = inj.get("team", {})
            results.append({
                "player":      f"{player.get('first_name')} {player.get('last_name')}",
                "team":        f"{team.get('city')} {team.get('name')}",
                "team_abbr":   team.get("abbreviation"),
                "status":      inj.get("status"),
                "return_date": inj.get("return_date", "Unknown"),
                "description": inj.get("description", ""),
            })
        return {"injuries": results, "count": len(results), "source": "BallDontLie"}
    except Exception as e:
        return {"error": str(e)}


def get_nba_standings() -> dict:
    try:
        season = datetime.now().year if datetime.now().month >= 10 else datetime.now().year - 1
        r = requests.get(
            f"{BALLDONTLIE_BASE_URL}/nba/v1/standings",
            headers=_bdl_headers(), params={"season": season}, timeout=10
        )
        teams = r.json().get("data", [])
        east, west = [], []
        for t in teams:
            team = t.get("team", {})
            entry = {
                "team": team.get("full_name"), "abbr": team.get("abbreviation"),
                "wins": t.get("wins"), "losses": t.get("losses"),
                "win_pct": t.get("win_pct"), "conf_rank": t.get("conference_rank"),
                "home": t.get("home_record"), "away": t.get("road_record"),
                "last_10": t.get("last_ten_games"), "streak": t.get("streak"),
                "conference": team.get("conference"),
            }
            (east if team.get("conference") == "East" else west).append(entry)
        east.sort(key=lambda x: x.get("conf_rank") or 99)
        west.sort(key=lambda x: x.get("conf_rank") or 99)
        return {"season": season, "eastern": east, "western": west, "source": "BallDontLie"}
    except Exception as e:
        return {"error": str(e)}


def get_nba_team_context(team_name: str) -> dict:
    """Full NBA team context: standing, last 10 games, injuries — parallel fetch."""
    team_name = team_name.strip()
    try:
        def fetch_recent_games():
            try:
                season = datetime.now().year if datetime.now().month >= 10 else datetime.now().year - 1
                r = requests.get(
                    f"{BALLDONTLIE_BASE_URL}/nba/v1/games",
                    headers=_bdl_headers(),
                    params={"seasons[]": season, "per_page": 100}, timeout=10
                )
                all_games = r.json().get("data", [])
                team_games = []
                for g in all_games:
                    home = g.get("home_team", {})
                    away = g.get("visitor_team", {})
                    hn = f"{home.get('city', '')} {home.get('name', '')}".lower()
                    vn = f"{away.get('city', '')} {away.get('name', '')}".lower()
                    if team_name.lower() in hn or team_name.lower() in vn:
                        team_games.append(g)
                final_games = [g for g in team_games if g.get("status") == "Final"]
                final_games.sort(key=lambda g: g.get("date", ""), reverse=True)
                recent = []
                for g in final_games[:10]:
                    home = g.get("home_team", {})
                    away = g.get("visitor_team", {})
                    hs = g.get("home_team_score", 0)
                    vs = g.get("visitor_team_score", 0)
                    hn = f"{home.get('city')} {home.get('name')}"
                    vn = f"{away.get('city')} {away.get('name')}"
                    is_home = team_name.lower() in hn.lower()
                    team_score = hs if is_home else vs
                    opp_score  = vs if is_home else hs
                    recent.append({
                        "date":      g.get("date"),
                        "opponent":  vn if is_home else hn,
                        "result":    "W" if team_score > opp_score else "L",
                        "score":     f"{team_score}-{opp_score}",
                        "home_away": "Home" if is_home else "Away"
                    })
                return recent
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=3) as ex:
            f_stand  = ex.submit(get_nba_standings)
            f_inj    = ex.submit(get_nba_injuries)
            f_recent = ex.submit(fetch_recent_games)
            standings_data = f_stand.result()
            injuries_data  = f_inj.result()
            recent_games   = f_recent.result()

        team_standing = None
        all_teams = standings_data.get("eastern", []) + standings_data.get("western", [])
        for t in all_teams:
            if team_name.lower() in (t.get("team") or "").lower():
                team_standing = t
                break

        team_injuries = [
            i for i in injuries_data.get("injuries", [])
            if team_name.lower() in (i.get("team") or "").lower()
        ]

        return {
            "team":          team_name,
            "standing":      team_standing,
            "recent_games":  recent_games,
            "injuries":      team_injuries,
            "injury_count":  len(team_injuries),
            "source":        "BallDontLie (parallel fetch)"
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
#  PAPER TRADING
# ─────────────────────────────────────────────
def place_paper_trade(symbol: str, side: str, dollar_amount: float, asset_type: str,
                      current_price: float, thesis: str = "", confidence: int = 0,
                      indicators: str = "", market_condition: str = "") -> dict:
    symbol = symbol.upper().replace("/USD", "").replace("USD", "")

    if db_has_open_position(symbol):
        return {"error": f"Position already open for {symbol}. Close it before opening a new one."}

    try:
        if asset_type == "crypto":
            order_data     = {"symbol": f"{symbol}/USD", "notional": str(round(dollar_amount, 2)),
                              "side": side, "type": "market", "time_in_force": "ioc"}
            estimated_cost = dollar_amount
            qty_display    = f"~{round(dollar_amount / current_price, 6)} {symbol}"
        elif asset_type == "stock":
            qty = int(dollar_amount / current_price)
            if qty < 1:
                return {"error": f"${dollar_amount} too small to buy 1 share at ${current_price:.2f}."}
            order_data     = {"symbol": symbol, "qty": str(qty), "side": side,
                              "type": "market", "time_in_force": "day"}
            estimated_cost = qty * current_price
            qty_display    = str(qty)
        else:  # option
            cpc = current_price * 100
            qty = int(dollar_amount / cpc)
            if qty < 1:
                return {"error": f"${dollar_amount} too small for 1 contract at ${current_price} premium."}
            order_data     = {"symbol": symbol, "qty": str(qty), "side": side,
                              "type": "market", "time_in_force": "day"}
            estimated_cost = qty * cpc
            qty_display    = str(qty)

        r      = requests.post(f"{ALPACA_TRADING_URL}/orders", headers=ALPACA_HEADERS,
                               json=order_data, timeout=10)
        result = r.json()

        if "id" in result:
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
                "source":         "Alpaca Paper Trading",
            }
        return {"error": result.get("message", "Order failed"), "details": result}
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
            "source": "Alpaca Paper Trading",
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
            "source":          "Alpaca Paper Trading",
        }
    except Exception as e:
        return {"error": str(e)}


def close_paper_position(symbol: str) -> dict:
    symbol     = symbol.upper().replace("/USD", "").replace("USD", "")
    api_symbol = f"{symbol}USD" if symbol in CRYPTO_SYMBOLS else symbol
    try:
        r = requests.delete(f"{ALPACA_TRADING_URL}/positions/{api_symbol}",
                            headers=ALPACA_HEADERS, timeout=10)
        if r.status_code in (200, 204):
            result  = r.json() if r.content else {}
            success = True
        else:
            result  = r.json() if r.content else {}
            success = "id" in result or "order_id" in result
        if success:
            try:
                pos_r      = requests.get(f"{ALPACA_TRADING_URL}/positions/{api_symbol}",
                                          headers=ALPACA_HEADERS, timeout=5)
                exit_price = float(pos_r.json().get("current_price", 0))
            except Exception:
                exit_price = 0
            db_log_exit(symbol, exit_price, "manual close")
            db_remove_monitor(symbol)
            with monitor_lock:
                trade_monitors.pop(symbol, None)
            return {"status": "✅ POSITION CLOSED", "symbol": symbol,
                    "note": "Paper trade closed and logged to journal."}
        return {"error": result.get("message", "Failed to close"), "details": result}
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
#  OPTIONS CHAIN
# ─────────────────────────────────────────────
def get_options_chain(ticker: str, option_type: str, expiration_date: str = None) -> dict:
    ticker      = ticker.upper()
    tradier_key = os.getenv("TRADIER_API_KEY")
    headers     = {"Authorization": f"Bearer {tradier_key}", "Accept": "application/json"}

    if not expiration_date:
        try:
            exp_r = requests.get(
                "https://api.tradier.com/v1/markets/options/expirations",
                headers=headers, params={"symbol": ticker, "includeAllRoots": "true"}, timeout=10)
            expirations = exp_r.json().get("expirations", {}).get("date", [])
            if not expirations:
                return {"error": f"No expirations found for {ticker}"}
            today = datetime.now().strftime("%Y-%m-%d")
            expiration_date = next((e for e in expirations if e >= today), expirations[0])
        except Exception as e:
            return {"error": f"Failed to get expirations: {e}"}

    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/options/chains",
            headers=headers,
            params={"symbol": ticker, "expiration": expiration_date, "greeks": "true"},
            timeout=10)
        options = r.json().get("options", {}).get("option", [])
        if not options:
            return {"error": f"No options found for {ticker} expiring {expiration_date}"}

        filtered = [o for o in options if o.get("option_type", "").lower() == option_type.lower()]

        try:
            snap  = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/snapshot",
                                 headers=ALPACA_HEADERS, timeout=8).json()
            price = snap.get("latestTrade", {}).get("p") or snap.get("dailyBar", {}).get("c") or 0
        except Exception:
            price = 0

        if price:
            filtered.sort(key=lambda o: abs(o.get("strike", 0) - price))
            filtered = filtered[:10]
            filtered.sort(key=lambda o: o.get("strike", 0))
        else:
            filtered = filtered[:10]

        contracts = [
            {
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
            }
            for o in filtered
        ]

        affordable = [c for c in contracts if c.get("ask") and c["ask"] <= MAX_OPTIONS_PREMIUM]
        rejected   = [c for c in contracts if c.get("ask") and c["ask"] > MAX_OPTIONS_PREMIUM]

        return {
            "ticker": ticker, "option_type": option_type, "expiration": expiration_date,
            "underlying_price": price, "contracts_found": len(affordable),
            "contracts": affordable, "rejected_expensive": len(rejected),
            "max_premium": MAX_OPTIONS_PREMIUM, "source": "Tradier",
            "note": f"Only showing contracts with ask <= ${MAX_OPTIONS_PREMIUM:.2f}.",
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
#  MONITOR TOOLS
# ─────────────────────────────────────────────
def set_trade_monitor(symbol: str, entry_price: float, asset_type: str,
                      stop_loss_pct: float, take_profit_pct: float, time_limit_min: int) -> dict:
    symbol = symbol.upper().replace("/USD", "")
    inserted = db_set_monitor(
        symbol=symbol, asset_type=asset_type, entry_price=entry_price,
        stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
        time_limit_min=time_limit_min, source="web"
    )
    if not inserted:
        return {"status": f"⚠️ Monitor already active for {symbol}. Not overwriting."}

    with monitor_lock:
        trade_monitors[symbol] = {
            "entry_price": entry_price, "entry_time": datetime.now(),
            "stop_loss_pct": stop_loss_pct, "take_profit_pct": take_profit_pct,
            "time_limit_min": time_limit_min, "asset_type": asset_type,
        }
    rules = []
    if stop_loss_pct:   rules.append(f"Stop loss: -{stop_loss_pct*100:.0f}%")
    if take_profit_pct: rules.append(f"Take profit: +{take_profit_pct*100:.0f}%")
    if time_limit_min:  rules.append(f"Time limit: {time_limit_min} min")
    return {"status": f"✅ Monitor active for {symbol}", "rules": rules,
            "note": "FinSight will auto-sell when any condition triggers. Checks every 60s."}


def get_monitor_log() -> dict:
    with monitor_lock:
        from core.database import db_get_active_monitors
        active = [r["symbol"] for r in db_get_active_monitors()]
        return {"log": list(monitor_log[-20:]) or ["No autonomous actions yet."],
                "active_monitors": active}


def cancel_trade_monitor(symbol: str) -> dict:
    symbol = symbol.upper().replace("/USD", "")
    db_remove_monitor(symbol)
    with monitor_lock:
        trade_monitors.pop(symbol, None)
    return {"status": f"✅ Monitor cancelled for {symbol}"}


def get_performance_summary() -> dict:
    return db_get_performance_summary()


def get_recent_trades(limit: int = 10) -> dict:
    trades = db_get_all_trades()[:limit]
    return {"trades": trades, "count": len(trades)}


# ─────────────────────────────────────────────
#  TOOL ROUTER
# ─────────────────────────────────────────────
def run_tool(tool_name: str, tool_input: dict) -> str:
    handlers = {
        # market data
        "get_stock_price":                  lambda: get_stock_price(**tool_input),
        "get_options_chain":                lambda: get_options_chain(**tool_input),
        "get_financial_news":               lambda: get_financial_news(**tool_input),
        "get_market_overview":              lambda: get_market_overview(),
        "get_stock_technicals":             lambda: get_stock_technicals(**tool_input),
        # paper trading
        "place_paper_trade":                lambda: place_paper_trade(**tool_input),
        "get_paper_positions":              lambda: get_paper_positions(),
        "get_paper_account":                lambda: get_paper_account(),
        "close_paper_position":             lambda: close_paper_position(**tool_input),
        # monitor + journal
        "set_trade_monitor":                lambda: set_trade_monitor(**tool_input),
        "get_monitor_log":                  lambda: get_monitor_log(),
        "cancel_trade_monitor":             lambda: cancel_trade_monitor(**tool_input),
        "get_performance_summary":          lambda: get_performance_summary(),
        "get_recent_trades":                lambda: get_recent_trades(**tool_input),
        # 0DTE analysis
        "get_trading_session":              lambda: get_trading_session(),
        "get_vwap":                         lambda: calculate_vwap(**tool_input),
        "get_expected_move":                lambda: calculate_expected_move(**tool_input),
        "get_0dte_context":                 lambda: get_0dte_context(**tool_input),
        # prediction markets (read)
        "get_kalshi_markets":               lambda: get_kalshi_markets(**tool_input),
        "get_kalshi_market_detail":         lambda: get_kalshi_market_detail(**tool_input),
        "analyze_prediction_market":        lambda: analyze_prediction_market(**tool_input),
        "calculate_bayesian_probability":   lambda: calculate_bayesian_probability(**tool_input),
        "calculate_kelly_criterion":        lambda: calculate_kelly_criterion(**tool_input),
        "get_prediction_market_categories": lambda: get_prediction_market_categories(),
        "get_polymarket_markets":           lambda: get_polymarket_markets(**tool_input),
        "get_polymarket_market_detail":     lambda: get_polymarket_market_detail(**tool_input),
        "search_prediction_markets":        lambda: search_prediction_markets(**tool_input),
        # Kalshi trading (real money)
        "get_kalshi_balance":               lambda: get_kalshi_balance(),
        "get_kalshi_positions":             lambda: get_kalshi_positions(),
        "get_kalshi_orders":                lambda: get_kalshi_orders(),
        "place_kalshi_order":               lambda: place_kalshi_order(**tool_input),
        "cancel_kalshi_order":              lambda: cancel_kalshi_order(**tool_input),
        "get_kalshi_events":                lambda: get_kalshi_events(**tool_input),
        "get_kalshi_sports_today":          lambda: get_kalshi_sports_today(),
        # NBA sports data
        "get_live_nba_games":               lambda: get_live_nba_games(),
        "get_nba_injuries":                 lambda: get_nba_injuries(),
        "get_nba_standings":                lambda: get_nba_standings(),
        "get_nba_team_context":             lambda: get_nba_team_context(**tool_input),
    }
    handler = handlers.get(tool_name)
    result  = handler() if handler else {"error": f"Unknown tool: {tool_name}"}
    output  = json.dumps(result)
    return output if output else json.dumps({"error": "Empty tool response"})
