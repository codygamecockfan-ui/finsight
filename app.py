import os
import json
import requests
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

# Market data endpoint (same for paper + live)
ALPACA_DATA_URL    = "https://data.alpaca.markets/v2"
# Paper trading endpoint
ALPACA_TRADING_URL = "https://paper-api.alpaca.markets/v2"

ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "Content-Type":        "application/json"
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
- ALWAYS ask the user for confirmation AND dollar amount before placing any trade.
- When a user says "place it", "execute", "do it", or confirms a trade, use place_paper_trade.
- For stocks: use the dollar amount to calculate share quantity.
- For options: use the option contract ticker from get_options_chain.
- After placing, always show the order confirmation details.
- Remind the user this is paper trading — no real money involved.

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
        "description": "Get the REAL-TIME stock price, bid/ask, and latest trade info for a ticker via Alpaca. Use this before any stock or options recommendation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol e.g. AAPL, TSLA, SPY"}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "get_options_chain",
        "description": "Get the options chain for a ticker including available strike prices and expiration dates. Use this when recommending options trades.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"},
                "expiration_date": {"type": "string", "description": "Options expiration date in YYYY-MM-DD format. Leave empty for next 8 days."},
                "option_type": {"type": "string", "enum": ["call", "put"], "description": "Type of option"}
            },
            "required": ["ticker", "option_type"]
        }
    },
    {
        "name": "get_financial_news",
        "description": "Get the latest financial news for a company, sector, or topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query e.g. 'Apple earnings', 'Federal Reserve interest rates'"},
                "num_articles": {"type": "integer", "description": "Number of articles to return (max 5)", "default": 3}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_market_overview",
        "description": "Get a broad market overview including major indices (SPY, QQQ, DIA, IWM) with REAL-TIME prices.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_stock_technicals",
        "description": "Get technical indicators and recent price history for a stock.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol"},
                "days": {"type": "integer", "description": "Number of days of history (default 30)", "default": 30}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "place_paper_trade",
        "description": "Place a PAPER trade (simulated, no real money) via Alpaca. Only call this after the user has explicitly confirmed the trade AND provided a dollar amount. Works for both stocks and options.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Stock ticker (e.g. AAPL) for stocks, or full option contract symbol (e.g. AAPL250321C00200000) for options"
                },
                "side": {
                    "type": "string",
                    "enum": ["buy", "sell"],
                    "description": "Buy or sell"
                },
                "dollar_amount": {
                    "type": "number",
                    "description": "Dollar amount to invest. Used to calculate quantity for stocks. For options, this is max spend on contracts."
                },
                "asset_type": {
                    "type": "string",
                    "enum": ["stock", "option"],
                    "description": "Whether this is a stock or option trade"
                },
                "current_price": {
                    "type": "number",
                    "description": "Current price of the stock or option premium. Used to calculate quantity."
                }
            },
            "required": ["symbol", "side", "dollar_amount", "asset_type", "current_price"]
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
        "description": "Close an open paper trade position. Use this when the user wants to exit a trade.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "The ticker symbol of the position to close"}
            },
            "required": ["symbol"]
        }
    }
]

# ─────────────────────────────────────────────
#  TOOL IMPLEMENTATIONS
# ─────────────────────────────────────────────

def get_stock_price(ticker: str) -> dict:
    ticker = ticker.upper()
    try:
        trade_r = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/trades/latest", headers=ALPACA_HEADERS, timeout=10)
        quote_r = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/quotes/latest", headers=ALPACA_HEADERS, timeout=10)
        snap_r  = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/snapshot",      headers=ALPACA_HEADERS, timeout=10)

        trade = trade_r.json().get("trade", {})
        quote = quote_r.json().get("quote", {})
        snap  = snap_r.json()
        daily = snap.get("dailyBar", {})
        prev  = snap.get("prevDailyBar", {})

        latest_price = trade.get("p") or daily.get("c")
        prev_close   = prev.get("c")
        change_pct   = round(((latest_price - prev_close) / prev_close) * 100, 2) if latest_price and prev_close else None

        return {
            "ticker":     ticker,
            "price":      latest_price,
            "bid":        quote.get("bp"),
            "ask":        quote.get("ap"),
            "open":       daily.get("o"),
            "high":       daily.get("h"),
            "low":        daily.get("l"),
            "prev_close": prev_close,
            "change_pct": change_pct,
            "volume":     daily.get("v"),
            "timestamp":  trade.get("t", datetime.now().isoformat()),
            "source":     "Alpaca Markets (real-time)"
        }
    except Exception as e:
        return {"error": str(e)}


def get_options_chain(ticker: str, option_type: str, expiration_date: str = None) -> dict:
    ticker = ticker.upper()
    params = {
        "underlying_ticker": ticker,
        "contract_type":     option_type,
        "limit":             10,
        "sort":              "strike_price",
        "order":             "asc",
        "apiKey":            POLYGON_API_KEY
    }
    if expiration_date:
        params["expiration_date"] = expiration_date
    else:
        params["expiration_date.gte"] = datetime.now().strftime("%Y-%m-%d")
        params["expiration_date.lte"] = (datetime.now() + timedelta(days=8)).strftime("%Y-%m-%d")
    try:
        r         = requests.get("https://api.polygon.io/v3/reference/options/contracts", params=params, timeout=10)
        contracts = r.json().get("results", [])
        if not contracts:
            return {"error": "No options contracts found. Try a different expiration or ticker."}
        return {
            "ticker":          ticker,
            "option_type":     option_type,
            "contracts_found": len(contracts),
            "contracts": [
                {
                    "contract_ticker":     c.get("ticker"),
                    "strike":              c.get("strike_price"),
                    "expiration":          c.get("expiration_date"),
                    "shares_per_contract": c.get("shares_per_contract", 100)
                }
                for c in contracts
            ],
            "source": "Polygon.io",
            "note":   "Use contract_ticker when placing an options trade."
        }
    except Exception as e:
        return {"error": str(e)}


def get_financial_news(query: str, num_articles: int = 3) -> dict:
    try:
        r        = requests.get("https://newsapi.org/v2/everything", params={
            "q": query, "language": "en", "sortBy": "publishedAt",
            "pageSize": min(num_articles, 5), "apiKey": NEWS_API_KEY
        }, timeout=10)
        articles = r.json().get("articles", [])
        if not articles:
            return {"error": "No articles found."}
        return {
            "query": query,
            "articles": [
                {
                    "title":     a["title"],
                    "source":    a["source"]["name"],
                    "published": a["publishedAt"],
                    "summary":   a.get("description", ""),
                    "url":       a["url"]
                }
                for a in articles
            ],
            "source": "NewsAPI.org"
        }
    except Exception as e:
        return {"error": str(e)}


def get_market_overview() -> dict:
    results = {}
    for ticker in ["SPY", "QQQ", "DIA", "IWM"]:
        try:
            snap  = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/snapshot", headers=ALPACA_HEADERS, timeout=10).json()
            daily = snap.get("dailyBar", {})
            prev  = snap.get("prevDailyBar", {})
            trade = snap.get("latestTrade", {})
            latest_price = trade.get("p") or daily.get("c")
            prev_close   = prev.get("c")
            results[ticker] = {
                "price":      latest_price,
                "open":       daily.get("o"),
                "high":       daily.get("h"),
                "low":        daily.get("l"),
                "prev_close": prev_close,
                "change_pct": round(((latest_price - prev_close) / prev_close) * 100, 2) if latest_price and prev_close else None,
                "volume":     daily.get("v")
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
    ticker     = ticker.upper()
    end_date   = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r       = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}?adjusted=true&sort=asc&apiKey={POLYGON_API_KEY}",
            timeout=10
        )
        results = r.json().get("results", [])
        if not results:
            return {"error": f"No historical data for {ticker}"}
        closes = [d["c"] for d in results]
        highs  = [d["h"] for d in results]
        lows   = [d["l"] for d in results]

        def sma(prices, period):
            return round(sum(prices[-period:]) / period, 2) if len(prices) >= period else None

        def rsi(closes, period=14):
            if len(closes) < period + 1:
                return None
            gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
            losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
            avg_g  = sum(gains[-period:]) / period
            avg_l  = sum(losses[-period:]) / period
            return 100 if avg_l == 0 else round(100 - (100 / (1 + avg_g / avg_l)), 2)

        return {
            "ticker": ticker, "period_days": days, "current_price": closes[-1],
            "52w_high": round(max(highs), 2), "52w_low": round(min(lows), 2),
            "sma_10": sma(closes, 10), "sma_20": sma(closes, 20), "sma_50": sma(closes, 50),
            "rsi_14": rsi(closes),
            "support": round(min(lows[-10:]), 2), "resistance": round(max(highs[-10:]), 2),
            "recent_5_days": [
                {"date": datetime.fromtimestamp(d["t"]/1000).strftime("%Y-%m-%d"),
                 "open": d["o"], "high": d["h"], "low": d["l"], "close": d["c"]}
                for d in results[-5:]
            ],
            "source": "Polygon.io (historical)"
        }
    except Exception as e:
        return {"error": str(e)}


def place_paper_trade(symbol: str, side: str, dollar_amount: float, asset_type: str, current_price: float) -> dict:
    symbol = symbol.upper()
    try:
        if asset_type == "stock":
            qty = int(dollar_amount / current_price)
            if qty < 1:
                return {"error": f"${dollar_amount} is too small to buy 1 share at ${current_price:.2f}. Need at least ${current_price:.2f}."}
        else:
            cost_per_contract = current_price * 100
            qty = int(dollar_amount / cost_per_contract)
            if qty < 1:
                return {"error": f"${dollar_amount} is too small for 1 contract at ${current_price} premium (${cost_per_contract:.2f}/contract)."}

        order_data = {
            "symbol":        symbol,
            "qty":           str(qty),
            "side":          side,
            "type":          "market",
            "time_in_force": "day"
        }

        r      = requests.post(f"{ALPACA_TRADING_URL}/orders", headers=ALPACA_HEADERS, json=order_data, timeout=10)
        result = r.json()

        if "id" in result:
            estimated_cost = float(result["qty"]) * current_price * (100 if asset_type == "option" else 1)
            return {
                "status":         "✅ PAPER ORDER PLACED",
                "order_id":       result["id"],
                "symbol":         result["symbol"],
                "side":           result["side"],
                "qty":            result["qty"],
                "type":           result["type"],
                "submitted_at":   result.get("submitted_at"),
                "estimated_cost": f"${estimated_cost:.2f}",
                "note":           "⚠️ This is a PAPER trade. No real money was used.",
                "source":         "Alpaca Paper Trading"
            }
        else:
            return {"error": result.get("message", "Order failed"), "details": result}

    except Exception as e:
        return {"error": str(e)}


def get_paper_positions() -> dict:
    try:
        r         = requests.get(f"{ALPACA_TRADING_URL}/positions", headers=ALPACA_HEADERS, timeout=10)
        positions = r.json()
        if not positions:
            return {"message": "No open positions in paper account.", "positions": []}
        return {
            "positions": [
                {
                    "symbol":         p["symbol"],
                    "qty":            p["qty"],
                    "side":           p["side"],
                    "entry_price":    p["avg_entry_price"],
                    "current_price":  p["current_price"],
                    "market_value":   p["market_value"],
                    "unrealized_pnl": p["unrealized_pl"],
                    "unrealized_pct": p["unrealized_plpc"]
                }
                for p in positions
            ],
            "source": "Alpaca Paper Trading"
        }
    except Exception as e:
        return {"error": str(e)}


def get_paper_account() -> dict:
    try:
        r    = requests.get(f"{ALPACA_TRADING_URL}/account", headers=ALPACA_HEADERS, timeout=10)
        acct = r.json()
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
    symbol = symbol.upper()
    try:
        r      = requests.delete(f"{ALPACA_TRADING_URL}/positions/{symbol}", headers=ALPACA_HEADERS, timeout=10)
        result = r.json()
        if "id" in result or "order_id" in result:
            return {
                "status":  "✅ POSITION CLOSED",
                "symbol":  symbol,
                "details": result,
                "note":    "Paper trade closed. No real money affected."
            }
        return {"error": result.get("message", "Failed to close position"), "details": result}
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
#  TOOL ROUTER
# ─────────────────────────────────────────────
def run_tool(tool_name: str, tool_input: dict) -> str:
    handlers = {
        "get_stock_price":      lambda: get_stock_price(**tool_input),
        "get_options_chain":    lambda: get_options_chain(**tool_input),
        "get_financial_news":   lambda: get_financial_news(**tool_input),
        "get_market_overview":  lambda: get_market_overview(),
        "get_stock_technicals": lambda: get_stock_technicals(**tool_input),
        "place_paper_trade":    lambda: place_paper_trade(**tool_input),
        "get_paper_positions":  lambda: get_paper_positions(),
        "get_paper_account":    lambda: get_paper_account(),
        "close_paper_position": lambda: close_paper_position(**tool_input),
    }
    handler = handlers.get(tool_name)
    result  = handler() if handler else {"error": f"Unknown tool: {tool_name}"}
    return json.dumps(result)


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
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     run_tool(block.name, block.input)
                    })
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


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    print(f"🚀 FinSight is running at http://localhost:{port}")
    app.run(debug=debug, host="0.0.0.0", port=port)
