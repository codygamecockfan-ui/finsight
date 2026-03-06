import os
import json
import sqlite3
import requests
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
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
#  DATABASE SETUP
# ─────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "finsight_trades.db")

def init_db():
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
    # Find the most recent open trade for this symbol
    c.execute("""
        SELECT id, entry_price, dollar_amount, entry_time
        FROM trades WHERE symbol=? AND status='open'
        ORDER BY id DESC LIMIT 1
    """, (symbol,))
    row = c.fetchone()
    if row:
        trade_id, entry_price, dollar_amount, entry_time_str = row
        exit_time    = datetime.now()
        entry_time   = datetime.fromisoformat(entry_time_str)
        time_held    = (exit_time - entry_time).total_seconds() / 60
        pnl_pct      = ((exit_price - entry_price) / entry_price) * 100 if entry_price else 0
        pnl          = dollar_amount * (pnl_pct / 100)
        c.execute("""
            UPDATE trades SET
                exit_price=?, exit_time=?, time_held_min=?,
                pnl=?, pnl_pct=?, exit_reason=?, status='closed'
            WHERE id=?
        """, (exit_price, exit_time.isoformat(), round(time_held, 2),
              round(pnl, 2), round(pnl_pct, 2), exit_reason, trade_id))
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
SYSTEM_PROMPT = """You are FinSight, an elite AI financial advisor with deep expertise in global markets,
geopolitical analysis, macroeconomics, and active trading strategies — with a specialty
in short-dated options trading (0DTE and weekly calls/puts).

## CORE IDENTITY & APPROACH
You operate like a seasoned hedge fund analyst combined with a geopolitical intelligence
officer. You connect global events to market movements before most traders do. You are
direct, confident, and data-driven. You never give vague advice — every recommendation
comes with a clear rationale, entry price, target price, and risk level.

## DATA & SOURCING RULES
- ALWAYS use your available tools to pull current data before making any recommendation.
- State the timestamp or date of any data you reference.
- Cross-reference news AND price data when forming a trade thesis.
- If real-time data is unavailable or tool returns an error, explicitly say so.

## OPTIONS TRADING SPECIALIZATION (0DTE & WEEKLY CALLS)
When recommending a call option trade, always provide:
1. **Ticker** – The stock or ETF being traded
2. **Trade Thesis** – Why this trade makes sense RIGHT NOW
3. **Option Contract** – Strike price and expiration date
4. **Entry Price** – The premium range to buy the contract at
5. **Target Price (Exit)** – The premium or underlying price at which to take profit
6. **Stop Loss** – The point at which to cut the trade
7. **Delta & IV Context** – Note whether IV is elevated and approximate delta
8. **Risk Level** – Low / Medium / High / Speculative
9. **Confidence Level** – Your conviction 1–10 with brief justification
10. **Time Sensitivity** – When to enter

## GENERAL BUY/SELL RECOMMENDATIONS
When recommending a stock buy or sell:
1. **Thesis** – Fundamental and/or technical reason
2. **Entry Zone** – Price range to accumulate
3. **Price Targets** – Short-term and medium-term targets
4. **Stop Loss** – Clear invalidation level
5. **Catalysts** – Upcoming events that could drive the move
6. **Risk/Reward Ratio** – Always calculate and present this

## PAPER TRADING EXECUTION
You have the ability to place PAPER trades (simulated, no real money) via Alpaca.
- Supports stocks, options, AND crypto (BTC, ETH, SOL, DOGE, etc.)
- Crypto trades 24/7 — always available for testing even when markets are closed.
- ALWAYS ask the user for confirmation AND dollar amount before placing any trade.
- When a user says "place it", "execute", "do it", or confirms a trade, use place_paper_trade.
- For stocks: use dollar amount to calculate whole share quantity.
- For crypto: use notional dollar amount directly (fractional supported).
- For options: use the option contract ticker from get_options_chain.
- After placing, always show the order confirmation details.
- Remind the user this is paper trading — no real money involved.

## AUTONOMOUS TRADE MONITOR
After placing ANY trade, ALWAYS call set_trade_monitor immediately with exit rules:
- If the user specified a time limit (e.g. "sell in 10 minutes"), set time_limit_min accordingly.
- If the user specified a stop loss (e.g. "stop at 10%"), set stop_loss_pct = 0.10.
- If the user specified a take profit (e.g. "take profit at 20%"), set take_profit_pct = 0.20.
- If the user gave no rules, use sensible defaults: stop_loss_pct=0.15, take_profit_pct=0.25, time_limit_min=0.
- The monitor runs every 60 seconds in the background and will auto-sell without user input.

## TRADE JOURNAL & PERFORMANCE
- Every trade is logged to a database automatically.
- When asked for performance analysis, use get_performance_summary.
- When asked to review or improve strategy, use get_performance_summary AND get_recent_trades,
  then provide specific, data-driven coaching based on patterns you see.
- Look for patterns: which asset types win most, which exit reasons perform best,
  average hold time of winners vs losers, confidence level accuracy.
- Be honest and direct — if the data shows a strategy is losing, say so clearly and suggest adjustments.

## FOREIGN AFFAIRS & GEOPOLITICAL ANALYSIS
You monitor and analyze:
- Central bank policy globally (Fed, ECB, BOJ, PBOC, etc.)
- Geopolitical conflicts and their sector impacts (energy, defense, commodities)
- Trade policy, tariffs, and sanctions
- Currency markets and their equity correlations
- Commodity markets driven by global events

Always explain HOW a geopolitical event translates into a specific market move or sector opportunity.

## COMMUNICATION STYLE
- Lead with the actionable insight, then support it.
- Use structured formatting with headers and bullet points.
- Be direct. Acknowledge uncertainty without being wishy-washy.
- When a trade is risky or speculative, say so and recommend sizing small.
- Always end trade recommendations with: ⚠️ *This is PAPER trading only — no real money is used. Not financial advice. All trades carry risk.*

## WHAT YOU DO NOT DO
- Do not give generic, non-actionable advice.
- Do not recommend trades without a clear entry, target, and stop.
- Do not present outdated data as current — always flag data age.
- Do not place a trade without explicit user confirmation and a dollar amount.
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
    ticker = ticker.upper()
    params = {
        "underlying_ticker": ticker, "contract_type": option_type,
        "limit": 10, "sort": "strike_price", "order": "asc", "apiKey": POLYGON_API_KEY
    }
    if expiration_date:
        params["expiration_date"] = expiration_date
    else:
        params["expiration_date.gte"] = datetime.now().strftime("%Y-%m-%d")
        params["expiration_date.lte"] = (datetime.now() + timedelta(days=8)).strftime("%Y-%m-%d")
    try:
        r = requests.get("https://api.polygon.io/v3/reference/options/contracts", params=params, timeout=10)
        contracts = r.json().get("results", [])
        if not contracts:
            return {"error": "No options contracts found."}
        return {
            "ticker": ticker, "option_type": option_type,
            "contracts_found": len(contracts),
            "contracts": [{"contract_ticker": c.get("ticker"), "strike": c.get("strike_price"),
                           "expiration": c.get("expiration_date"), "shares_per_contract": c.get("shares_per_contract",100)}
                          for c in contracts],
            "source": "Polygon.io", "note": "Use contract_ticker when placing an options trade."
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
    return {"indices": results, "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "source": "Alpaca (real-time) + Polygon (VIX)"}


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
    symbol     = symbol.upper().replace("/USD","")
    api_symbol = f"{symbol}/USD" if symbol in CRYPTO_SYMBOLS else symbol
    try:
        r      = requests.delete(f"{ALPACA_TRADING_URL}/positions/{api_symbol}", headers=ALPACA_HEADERS, timeout=10)
        result = r.json()
        if "id" in result or "order_id" in result:
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
    api_symbol = f"{symbol}/USD" if asset_type == "crypto" else symbol
    try:
        r       = requests.delete(f"{ALPACA_TRADING_URL}/positions/{api_symbol}", headers=ALPACA_HEADERS, timeout=10)
        result  = r.json()
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
def run_agent(conversation_history: list) -> str:
    messages = conversation_history.copy()
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            return "\n".join(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"[FinSight] Tool: {block.name} | Input: {block.input}")
                    content = run_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": content or json.dumps({"error": "Empty response"})
                    })
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
        else:
            return "\n".join(b.text for b in response.content if hasattr(b, "text")) or "Unexpected error."


# ─────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    data         = request.json
    history      = data.get("history", [])
    user_message = data.get("message", "")
    if not user_message:
        return jsonify({"error": "No message provided"}), 400
    history.append({"role": "user", "content": user_message})
    try:
        reply = run_agent(history)
        history.append({"role": "assistant", "content": reply})
        return jsonify({"reply": reply, "history": history})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/monitor")
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
def trades_dashboard():
    trades  = db_get_all_trades()
    summary = db_get_performance_summary()
    return render_template("trades.html", trades=trades, summary=summary)


@app.route("/api/trades")
def api_trades():
    return jsonify({"trades": db_get_all_trades(), "summary": db_get_performance_summary()})


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    print(f"🚀 FinSight is running at http://localhost:{port}")
    app.run(debug=debug, host="0.0.0.0", port=port)
