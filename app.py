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

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
POLYGON_API_KEY   = os.getenv("POLYGON_API_KEY")
NEWS_API_KEY      = os.getenv("NEWS_API_KEY")

# ─────────────────────────────────────────────
#  SYSTEM PROMPT
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """You are FinSight, an elite AI financial advisor with deep expertise in global markets,
geopolitical analysis, macroeconomics, and active trading strategies — with a specialty
in short-dated options trading (0DTE and weekly calls/puts).

## WHO YOU ARE
You're like that one buddy who grew up on a farm, drives a beat-up F-150, drinks Busch Light,
and somehow knows more about the stock market than anyone on Wall Street. You're a straight-shooting,
blue collar trading genius who calls it like he sees it. You cuss occasionally when it feels right —
nothing crazy, just enough to keep it real. You use folksy analogies, redneck humor, and southern 
expressions to explain complex financial concepts. Think less "CNBC anchor" and more "guy at the 
deer camp who just made 40% on a SPY call."

You're laid back and casual but when it comes to the actual numbers and trade setups — you are 
dead serious and razor sharp. The humor is in the delivery, not the advice.

Examples of your voice:
- "Boy I'll tell you what, this chart looks cleaner than a freshly waxed truck bed."
- "That earnings miss was uglier than a mud fence in a rainstorm — stay the hell away."
- "This setup's tighter than a new pair of wranglers. Here's what I'd do..."
- "Hell yeah this looks good. Let me break it down for ya."
- "I wouldn't touch that trade with a 10 foot cattle prod."
- "That IV is so damn high it'd make your head spin."

The user's name is Cody. Use his name occasionally like a buddy would — not every message, 
just when it feels natural.

If Cody asks about a bad trade idea, roast it lovingly before explaining why it's a bad idea.
If Cody asks about a great setup, get genuinely fired up about it.
If the market is boring, say so. Don't dress up nothing as something.

## CORE APPROACH
You connect global events to market movements before most traders do. Every recommendation 
comes with a clear rationale, entry price, target price, risk level, and confidence score.
No vague BS — you're a straight shooter.

## DATA & SOURCING RULES
- ALWAYS use your available tools to pull current data before making any recommendation.
- State the timestamp or date of any data you reference.
- Cross-reference news AND price data when forming a trade thesis.
- If real-time data is unavailable or tool returns an error, say so straight up.

## OPTIONS TRADING SPECIALIZATION (0DTE & WEEKLY CALLS)
When recommending a call option trade, always provide:
1. **Ticker** – The stock or ETF
2. **Trade Thesis** – Why this trade makes sense RIGHT NOW, in plain english
3. **Option Contract** – Strike price and expiration date
4. **Entry Price** – The premium range to buy at
5. **Target Price (Exit)** – Where to take profit
6. **Stop Loss** – Where to cut it and walk away
7. **Delta & IV Context** – Is IV jacked up? What's the delta?
8. **Risk Level** – Low / Medium / High / Speculative
9. **Confidence Level** – Your gut feeling 1–10 with a one-liner reason. Be honest — don't hype garbage.
10. **Time Sensitivity** – When to get in

## GENERAL BUY/SELL RECOMMENDATIONS
When recommending a stock buy or sell always include:
1. **Thesis** – Why in plain english
2. **Entry Zone** – Price range to buy
3. **Price Targets** – Short term and medium term
4. **Stop Loss** – Where you're wrong
5. **Catalysts** – What's gonna move it
6. **Risk/Reward Ratio** – Always do the math
7. **Confidence Level** – 1–10 with a quick reason

## FOREIGN AFFAIRS & GEOPOLITICAL ANALYSIS
You keep an eye on:
- Central banks (Fed, ECB, BOJ, PBOC etc.)
- Wars, conflicts, and how they hit energy, defense, commodities
- Tariffs, trade deals, sanctions
- Currency moves and how they ripple into stocks
- Commodity markets

Always connect the dots — explain HOW a world event turns into a trade opportunity.

## COMMUNICATION STYLE
- Talk like a smart redneck buddy, not a financial textbook
- Cuss occasionally when it fits — keep it natural not forced
- Use blue collar analogies to explain complex stuff
- Lead with the actionable take, then back it up
- Be direct — don't hedge everything to death
- When a trade is risky, say "size small on this one" or "don't bet the farm"
- Always end trade recommendations with: ⚠️ *This ain't official financial advice partner. All trades carry risk. Size your positions right and maybe talk to a real advisor for the big stuff.*

## WHAT YOU DON'T DO
- Give wishy washy non-answers
- Recommend trades without entry, target, and stop
- Pass off old data as fresh — always flag how recent the data is
- Pretend a bad setup is good just to have something to say
"""

# ─────────────────────────────────────────────
#  TOOL DEFINITIONS (sent to Claude)
# ─────────────────────────────────────────────
TOOLS = [
    {
        "name": "get_stock_price",
        "description": "Get the latest stock price, daily open/close, volume, and basic info for a ticker. Use this before any stock or options recommendation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker symbol e.g. AAPL, TSLA, SPY"
                }
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
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker symbol"
                },
                "expiration_date": {
                    "type": "string",
                    "description": "Options expiration date in YYYY-MM-DD format. Leave empty to get next available expirations."
                },
                "option_type": {
                    "type": "string",
                    "enum": ["call", "put"],
                    "description": "Type of option"
                }
            },
            "required": ["ticker", "option_type"]
        }
    },
    {
        "name": "get_financial_news",
        "description": "Get the latest financial news for a company, sector, or topic. Use this to build trade thesis and identify catalysts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query e.g. 'Apple earnings', 'Federal Reserve interest rates', 'oil prices geopolitics'"
                },
                "num_articles": {
                    "type": "integer",
                    "description": "Number of articles to return (max 5)",
                    "default": 3
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_market_overview",
        "description": "Get a broad market overview including major indices (SPY, QQQ, DIA, IWM) prices. Use this for general market context.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_stock_technicals",
        "description": "Get technical indicators and recent price history for a stock to assist with support/resistance and entry/exit analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker symbol"
                },
                "days": {
                    "type": "integer",
                    "description": "Number of days of history to retrieve (default 30)",
                    "default": 30
                }
            },
            "required": ["ticker"]
        }
    }
]

# ─────────────────────────────────────────────
#  TOOL IMPLEMENTATIONS
# ─────────────────────────────────────────────

def get_stock_price(ticker: str) -> dict:
    ticker = ticker.upper()
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?adjusted=true&apiKey={POLYGON_API_KEY}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("resultsCount", 0) == 0:
            return {"error": f"No data found for {ticker}"}
        result = data["results"][0]
        return {
            "ticker": ticker,
            "date": datetime.fromtimestamp(result["t"] / 1000).strftime("%Y-%m-%d"),
            "open":   result["o"],
            "high":   result["h"],
            "low":    result["l"],
            "close":  result["c"],
            "volume": result["v"],
            "vwap":   result.get("vw"),
            "source": "Polygon.io"
        }
    except Exception as e:
        return {"error": str(e)}


def get_options_chain(ticker: str, option_type: str, expiration_date: str = None) -> dict:
    ticker = ticker.upper()
    params = {
        "underlying_ticker": ticker,
        "contract_type": option_type,
        "limit": 10,
        "sort": "strike_price",
        "order": "asc",
        "apiKey": POLYGON_API_KEY
    }
    if expiration_date:
        params["expiration_date"] = expiration_date
    else:
        # Get options expiring within the next 8 days (0DTE to weekly)
        params["expiration_date.gte"] = datetime.now().strftime("%Y-%m-%d")
        params["expiration_date.lte"] = (datetime.now() + timedelta(days=8)).strftime("%Y-%m-%d")

    url = "https://api.polygon.io/v3/reference/options/contracts"
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        contracts = data.get("results", [])
        if not contracts:
            return {"error": "No options contracts found for the given parameters. Try a different expiration or ticker."}
        return {
            "ticker": ticker,
            "option_type": option_type,
            "contracts_found": len(contracts),
            "contracts": [
                {
                    "contract_ticker": c.get("ticker"),
                    "strike": c.get("strike_price"),
                    "expiration": c.get("expiration_date"),
                    "shares_per_contract": c.get("shares_per_contract", 100)
                }
                for c in contracts
            ],
            "source": "Polygon.io",
            "note": "Use Polygon.io or your broker's platform to get live bid/ask premiums for these contracts."
        }
    except Exception as e:
        return {"error": str(e)}


def get_financial_news(query: str, num_articles: int = 3) -> dict:
    num_articles = min(num_articles, 5)
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": num_articles,
        "apiKey": NEWS_API_KEY
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        articles = data.get("articles", [])
        if not articles:
            return {"error": "No articles found for that query."}
        return {
            "query": query,
            "articles": [
                {
                    "title": a["title"],
                    "source": a["source"]["name"],
                    "published": a["publishedAt"],
                    "summary": a.get("description", "No description available."),
                    "url": a["url"]
                }
                for a in articles
            ],
            "source": "NewsAPI.org"
        }
    except Exception as e:
        return {"error": str(e)}


def get_market_overview() -> dict:
    indices = ["SPY", "QQQ", "DIA", "IWM", "VIX"]
    results = {}
    for ticker in indices:
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?adjusted=true&apiKey={POLYGON_API_KEY}"
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            if data.get("resultsCount", 0) > 0:
                res = data["results"][0]
                change_pct = ((res["c"] - res["o"]) / res["o"]) * 100
                results[ticker] = {
                    "close": res["c"],
                    "open": res["o"],
                    "change_pct": round(change_pct, 2),
                    "volume": res["v"]
                }
        except:
            results[ticker] = {"error": "Failed to fetch"}
    return {
        "indices": results,
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "source": "Polygon.io",
        "note": "Prices are from the previous trading day's close (Polygon free tier)."
    }


def get_stock_technicals(ticker: str, days: int = 30) -> dict:
    ticker = ticker.upper()
    end_date   = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}?adjusted=true&sort=asc&apiKey={POLYGON_API_KEY}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        results = data.get("results", [])
        if not results:
            return {"error": f"No historical data for {ticker}"}

        closes = [d["c"] for d in results]
        highs  = [d["h"] for d in results]
        lows   = [d["l"] for d in results]

        # Simple moving averages
        def sma(prices, period):
            if len(prices) < period:
                return None
            return round(sum(prices[-period:]) / period, 2)

        # RSI (14-period)
        def rsi(closes, period=14):
            if len(closes) < period + 1:
                return None
            gains, losses = [], []
            for i in range(1, len(closes)):
                delta = closes[i] - closes[i-1]
                gains.append(max(delta, 0))
                losses.append(max(-delta, 0))
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            if avg_loss == 0:
                return 100
            rs = avg_gain / avg_loss
            return round(100 - (100 / (1 + rs)), 2)

        recent = results[-5:]
        return {
            "ticker": ticker,
            "period_days": days,
            "current_price": closes[-1],
            "52w_high": round(max(highs), 2),
            "52w_low": round(min(lows), 2),
            "sma_10": sma(closes, 10),
            "sma_20": sma(closes, 20),
            "sma_50": sma(closes, 50),
            "rsi_14": rsi(closes),
            "support": round(min(lows[-10:]), 2),
            "resistance": round(max(highs[-10:]), 2),
            "recent_5_days": [
                {
                    "date": datetime.fromtimestamp(d["t"] / 1000).strftime("%Y-%m-%d"),
                    "open": d["o"], "high": d["h"], "low": d["l"], "close": d["c"]
                }
                for d in recent
            ],
            "source": "Polygon.io"
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
#  TOOL ROUTER
# ─────────────────────────────────────────────
def run_tool(tool_name: str, tool_input: dict) -> str:
    if tool_name == "get_stock_price":
        result = get_stock_price(**tool_input)
    elif tool_name == "get_options_chain":
        result = get_options_chain(**tool_input)
    elif tool_name == "get_financial_news":
        result = get_financial_news(**tool_input)
    elif tool_name == "get_market_overview":
        result = get_market_overview()
    elif tool_name == "get_stock_technicals":
        result = get_stock_technicals(**tool_input)
    else:
        result = {"error": f"Unknown tool: {tool_name}"}
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

        # Append assistant's response to history
        messages.append({"role": "assistant", "content": response.content})

        # If Claude is done (no tool calls), return the text
        if response.stop_reason == "end_turn":
            text_blocks = [b.text for b in response.content if hasattr(b, "text")]
            return "\n".join(text_blocks)

        # Process tool calls
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"[FinSight] Calling tool: {block.name} with {block.input}")
                    result = run_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

            # Feed results back to Claude
            messages.append({"role": "user", "content": tool_results})
        else:
            # Unexpected stop reason
            text_blocks = [b.text for b in response.content if hasattr(b, "text")]
            return "\n".join(text_blocks) or "An unexpected error occurred."


# ─────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────
APP_PASSWORD = os.getenv("APP_PASSWORD", "finsight2024")

from flask import session
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey_changeme")

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authenticated"] = True
            return redirect("/")
        error = "Incorrect password. Try again."
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>FinSight Login</title>
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{ background: #0a0e1a; color: #e2e8f0; font-family: system-ui, sans-serif;
                   display: flex; align-items: center; justify-content: center; height: 100vh; }}
            .box {{ background: #111827; border: 1px solid #1f2d45; border-radius: 16px;
                   padding: 40px; width: 340px; text-align: center; }}
            .logo {{ width: 48px; height: 48px; background: linear-gradient(135deg, #00d4aa, #3b82f6);
                    border-radius: 12px; display: flex; align-items: center; justify-content: center;
                    font-size: 20px; font-weight: bold; color: #fff; margin: 0 auto 16px; }}
            h1 {{ font-size: 1.4rem; margin-bottom: 6px; }}
            p {{ color: #64748b; font-size: .85rem; margin-bottom: 24px; }}
            input {{ width: 100%; background: #0a0e1a; border: 1px solid #1f2d45; color: #e2e8f0;
                    padding: 12px 16px; border-radius: 10px; font-size: .95rem; margin-bottom: 12px; outline: none; }}
            input:focus {{ border-color: #00d4aa; }}
            button {{ width: 100%; background: linear-gradient(135deg, #00d4aa, #3b82f6);
                     border: none; border-radius: 10px; color: #fff; padding: 12px;
                     font-size: 1rem; font-weight: 600; cursor: pointer; }}
            .error {{ color: #f87171; font-size: .82rem; margin-bottom: 12px; }}
        </style>
    </head>
    <body>
        <div class="box">
            <div class="logo">FS</div>
            <h1>FinSight</h1>
            <p>Enter your password to continue</p>
            {"<div class='error'>" + error + "</div>" if error else ""}
            <form method="POST">
                <input type="password" name="password" placeholder="Password" autofocus />
                <button type="submit">Enter FinSight</button>
            </form>
        </div>
    </body>
    </html>
    """

from flask import redirect

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/")
def index():
    if not session.get("authenticated"):
        return redirect("/login")
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    if not session.get("authenticated"):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    history = data.get("history", [])
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
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    print(f"🚀 FinSight is running at http://localhost:{port}")
    app.run(debug=debug, host="0.0.0.0", port=port)
