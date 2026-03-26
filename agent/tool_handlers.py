"""
Implementations for all Claude tools callable from the agent loop.
"""
import os
import json
import requests
from datetime import datetime
from core.config import (
    ALPACA_TRADING_URL, ALPACA_DATA_URL, ALPACA_HEADERS,
    CRYPTO_SYMBOLS, MAX_OPTIONS_PREMIUM, TRADE_CONFIDENCE_THRESHOLD, AUTO_EXECUTE_MAX_DOLLAR
)
from core.database import (
    db_log_entry, db_log_exit, db_get_all_trades, db_get_performance_summary,
    db_set_monitor, db_remove_monitor, db_has_open_position
)
from core.market_data import get_stock_price, get_stock_technicals, get_market_overview, get_financial_news
from core.analysis import get_trading_session, calculate_vwap, calculate_expected_move, get_0dte_context
from core.prediction import (
    get_kalshi_markets, get_kalshi_market_detail, analyze_prediction_market,
    calculate_bayesian_probability, calculate_kelly_criterion, get_prediction_market_categories,
    get_polymarket_markets, get_polymarket_market_detail, search_prediction_markets
)
from core.monitor import trade_monitors, monitor_lock, monitor_log


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


def set_trade_monitor(symbol: str, entry_price: float, asset_type: str,
                      stop_loss_pct: float, take_profit_pct: float, time_limit_min: int) -> dict:
    symbol = symbol.upper().replace("/USD", "")
    inserted = db_set_monitor(
        symbol=symbol, asset_type=asset_type, entry_price=entry_price,
        stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
        time_limit_min=time_limit_min, source="web"
    )
    if not inserted:
        return {"status": f"⚠️ Monitor already active for {symbol} (possibly set by scheduler). Not overwriting."}

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


# ── Tool router ──────────────────────────────────────────────

def run_tool(tool_name: str, tool_input: dict) -> str:
    handlers = {
        "get_stock_price":                  lambda: get_stock_price(**tool_input),
        "get_options_chain":                lambda: get_options_chain(**tool_input),
        "get_financial_news":               lambda: get_financial_news(**tool_input),
        "get_market_overview":              lambda: get_market_overview(),
        "get_stock_technicals":             lambda: get_stock_technicals(**tool_input),
        "place_paper_trade":                lambda: place_paper_trade(**tool_input),
        "get_paper_positions":              lambda: get_paper_positions(),
        "get_paper_account":                lambda: get_paper_account(),
        "close_paper_position":             lambda: close_paper_position(**tool_input),
        "set_trade_monitor":                lambda: set_trade_monitor(**tool_input),
        "get_monitor_log":                  lambda: get_monitor_log(),
        "cancel_trade_monitor":             lambda: cancel_trade_monitor(**tool_input),
        "get_performance_summary":          lambda: get_performance_summary(),
        "get_recent_trades":                lambda: get_recent_trades(**tool_input),
        "get_trading_session":              lambda: get_trading_session(),
        "get_vwap":                         lambda: calculate_vwap(**tool_input),
        "get_expected_move":                lambda: calculate_expected_move(**tool_input),
        "get_kalshi_markets":               lambda: get_kalshi_markets(**tool_input),
        "get_kalshi_market_detail":         lambda: get_kalshi_market_detail(**tool_input),
        "analyze_prediction_market":        lambda: analyze_prediction_market(**tool_input),
        "calculate_bayesian_probability":   lambda: calculate_bayesian_probability(**tool_input),
        "calculate_kelly_criterion":        lambda: calculate_kelly_criterion(**tool_input),
        "get_prediction_market_categories": lambda: get_prediction_market_categories(),
        "get_polymarket_markets":           lambda: get_polymarket_markets(**tool_input),
        "get_polymarket_market_detail":     lambda: get_polymarket_market_detail(**tool_input),
        "search_prediction_markets":        lambda: search_prediction_markets(**tool_input),
        "get_0dte_context":                 lambda: get_0dte_context(**tool_input),
    }
    handler = handlers.get(tool_name)
    result  = handler() if handler else {"error": f"Unknown tool: {tool_name}"}
    output  = json.dumps(result)
    return output if output else json.dumps({"error": "Empty tool response"})
