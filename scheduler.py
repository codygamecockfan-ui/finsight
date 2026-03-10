"""
FinSight Autonomous Trading Scheduler
Runs at 9:31AM, 12:00PM, and 3:30PM ET on weekdays.
Only places trades when ALL criteria are met:
  - Confidence 7/10 or higher
  - Technical momentum confirmed
  - Strong news catalyst exists
Max trade size controlled by MAX_TRADE_AMOUNT env var (default $500).
To go live: change ALPACA_TRADING_URL to https://api.alpaca.markets/v2
"""

import os
import json
import sqlite3
import requests
import schedule
import time
from datetime import datetime, timedelta
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic()

POLYGON_API_KEY    = os.getenv("POLYGON_API_KEY")
NEWS_API_KEY       = os.getenv("NEWS_API_KEY")
ALPACA_API_KEY     = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = os.getenv("ALPACA_SECRET_KEY")

ALPACA_DATA_URL    = "https://data.alpaca.markets/v2"
ALPACA_CRYPTO_URL  = "https://data.alpaca.markets/v1beta3/crypto/us"
ALPACA_TRADING_URL = "https://paper-api.alpaca.markets/v2"  # ← swap to api.alpaca.markets for live

CRYPTO_SYMBOLS = {"BTC","ETH","SOL","DOGE","AVAX","LINK","MATIC","LTC","BCH","XRP","ADA"}

ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "Content-Type":        "application/json"
}

MAX_TRADE_AMOUNT = float(os.getenv("MAX_TRADE_AMOUNT", "500"))
DB_PATH          = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "finsight_trades.db"))


# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
def log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[Scheduler] [{ts}] {msg}"
    print(line, flush=True)
    try:
        with open("scheduler.log", "a") as f:
            f.write(line + "\n")
    except:
        pass


# ─────────────────────────────────────────────
#  DATABASE  (mirrors app.py schema)
# ─────────────────────────────────────────────
def db_log_entry(symbol, asset_type, side, entry_price, qty, dollar_amount,
                 thesis="", confidence=0, indicators="", market_condition="", order_id=""):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO trades
            (symbol,asset_type,side,entry_price,qty,dollar_amount,
             thesis,confidence,indicators,market_condition,entry_time,order_id,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open')
        """, (symbol, asset_type, side, entry_price, qty, dollar_amount,
              thesis, confidence, indicators, market_condition,
              datetime.now().isoformat(), order_id))
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"DB entry error: {e}")


def db_log_exit(symbol, exit_price, exit_reason):
    try:
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("""
            SELECT id, entry_price, dollar_amount, entry_time
            FROM trades WHERE symbol=? AND status='open'
            ORDER BY id DESC LIMIT 1
        """, (symbol,))
        row = c.fetchone()
        if row:
            tid, ep, da, et_str = row
            exit_time = datetime.now()
            held      = (exit_time - datetime.fromisoformat(et_str)).total_seconds() / 60
            pct       = ((exit_price - ep) / ep) * 100 if ep else 0
            pnl       = da * (pct / 100)
            c.execute("""
                UPDATE trades SET exit_price=?,exit_time=?,time_held_min=?,
                pnl=?,pnl_pct=?,exit_reason=?,status='closed' WHERE id=?
            """, (exit_price, exit_time.isoformat(), round(held,2),
                  round(pnl,2), round(pct,2), exit_reason, tid))
            conn.commit()
        conn.close()
    except Exception as e:
        log(f"DB exit error: {e}")


# ─────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────
def fetch_market_snapshot() -> dict:
    snapshot = {}

    # Indices
    indices = {}
    for t in ["SPY","QQQ","IWM","DIA"]:
        try:
            s     = requests.get(f"{ALPACA_DATA_URL}/stocks/{t}/snapshot", headers=ALPACA_HEADERS, timeout=10).json()
            daily = s.get("dailyBar", {})
            prev  = s.get("prevDailyBar", {})
            trade = s.get("latestTrade", {})
            lp    = trade.get("p") or daily.get("c")
            pc    = prev.get("c")
            indices[t] = {
                "price": lp, "open": daily.get("o"),
                "high": daily.get("h"), "low": daily.get("l"),
                "change_pct": round(((lp-pc)/pc)*100, 2) if lp and pc else None,
                "volume": daily.get("v")
            }
        except Exception as e:
            indices[t] = {"error": str(e)}
    snapshot["indices"] = indices

    # VIX
    try:
        vr = requests.get(f"https://api.polygon.io/v2/aggs/ticker/VIX/prev?adjusted=true&apiKey={POLYGON_API_KEY}", timeout=10).json()
        if vr.get("resultsCount", 0) > 0:
            snapshot["VIX"] = vr["results"][0]["c"]
    except:
        snapshot["VIX"] = "unavailable"

    # Top crypto
    crypto_prices = {}
    for sym in ["BTC","ETH","SOL","DOGE"]:
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
                "change_pct": round(((lp-pc)/pc)*100, 2) if lp and pc else None,
                "volume": d.get("v")
            }
        except Exception as e:
            crypto_prices[sym] = {"error": str(e)}
    snapshot["crypto"] = crypto_prices

    # Top news
    try:
        nr = requests.get("https://newsapi.org/v2/top-headlines", params={
            "category": "business", "language": "en",
            "pageSize": 5, "apiKey": NEWS_API_KEY
        }, timeout=10).json()
        snapshot["top_news"] = [
            {"title": a["title"], "source": a["source"]["name"], "published": a["publishedAt"]}
            for a in nr.get("articles", [])[:5]
        ]
    except:
        snapshot["top_news"] = []

    snapshot["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S ET")
    return snapshot


def fetch_technicals(ticker: str, days: int = 20) -> dict:
    ticker     = ticker.upper()
    end_date   = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r       = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}"
            f"?adjusted=true&sort=asc&apiKey={POLYGON_API_KEY}", timeout=10)
        results = r.json().get("results", [])
        if not results:
            return {"error": "No data"}
        closes = [d["c"] for d in results]

        def sma(n):
            return round(sum(closes[-n:])/n, 2) if len(closes) >= n else None

        def rsi(n=14):
            if len(closes) < n+1: return None
            gains  = [max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
            losses = [max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
            ag, al = sum(gains[-n:])/n, sum(losses[-n:])/n
            return 100 if al==0 else round(100-(100/(1+ag/al)),2)

        return {
            "ticker": ticker, "current": closes[-1],
            "sma_10": sma(10), "sma_20": sma(20),
            "rsi_14": rsi(),
            "above_sma10": closes[-1] > sma(10) if sma(10) else None,
            "above_sma20": closes[-1] > sma(20) if sma(20) else None,
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
#  ORDER EXECUTION
# ─────────────────────────────────────────────
def place_order(symbol: str, asset_type: str, side: str,
                dollar_amount: float, current_price: float) -> dict:
    symbol = symbol.upper().replace("/USD","")
    try:
        if asset_type == "crypto":
            order_data = {
                "symbol": f"{symbol}/USD",
                "notional": str(round(dollar_amount, 2)),
                "side": side, "type": "market", "time_in_force": "ioc"
            }
            qty_display = f"~{round(dollar_amount/current_price,6)} {symbol}"
        else:
            qty = int(dollar_amount / current_price)
            if qty < 1:
                return {"error": f"${dollar_amount} too small for 1 share at ${current_price:.2f}"}
            order_data  = {"symbol": symbol, "qty": str(qty), "side": side,
                           "type": "market", "time_in_force": "day"}
            qty_display = str(qty)

        r      = requests.post(f"{ALPACA_TRADING_URL}/orders", headers=ALPACA_HEADERS,
                               json=order_data, timeout=10)
        result = r.json()
        if "id" in result:
            return {"success": True, "order_id": result["id"],
                    "symbol": symbol, "qty": qty_display,
                    "estimated_cost": dollar_amount}
        return {"success": False, "error": result.get("message","Order failed")}
    except Exception as e:
        return {"success": False, "error": str(e)}


def close_position(symbol: str, asset_type: str) -> dict:
    api_symbol = f"{symbol}/USD" if asset_type == "crypto" else symbol
    try:
        r = requests.delete(f"{ALPACA_TRADING_URL}/positions/{api_symbol}",
                            headers=ALPACA_HEADERS, timeout=10)
        if r.status_code in (200, 204):
            return {"success": True}
        result = r.json() if r.content else {}
        return {"success": False, "error": result.get("message","Failed")}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_current_price(symbol: str, asset_type: str) -> float | None:
    try:
        if asset_type == "crypto":
            pair = f"{symbol}/USD"
            s    = requests.get(f"{ALPACA_CRYPTO_URL}/snapshots", headers=ALPACA_HEADERS,
                                params={"symbols": pair}, timeout=8).json()
            snap = s.get("snapshots", {}).get(pair, {})
            return snap.get("latestTrade",{}).get("p") or snap.get("dailyBar",{}).get("c")
        else:
            s = requests.get(f"{ALPACA_DATA_URL}/stocks/{symbol}/snapshot",
                             headers=ALPACA_HEADERS, timeout=8).json()
            return s.get("latestTrade",{}).get("p") or s.get("dailyBar",{}).get("c")
    except:
        return None


# ─────────────────────────────────────────────
#  CLAUDE ANALYSIS
# ─────────────────────────────────────────────
SCHEDULER_SYSTEM_PROMPT = """You are FinSight, an autonomous trading agent.
You have been given a market snapshot and must decide whether to place a trade.

STRICT CRITERIA — only recommend a trade if ALL THREE are met:
1. Confidence 7/10 or higher
2. Clear technical momentum (price above key MAs, RSI in favorable range, clear trend)
3. Strong news catalyst that has NOT fully priced in yet

If criteria are not met, respond with NO_TRADE and explain why.

When you DO recommend a trade, respond ONLY with valid JSON in this exact format:
{
  "action": "TRADE",
  "symbol": "TICKER",
  "asset_type": "stock" or "crypto",
  "side": "buy" or "sell",
  "dollar_amount": number (max """ + str(MAX_TRADE_AMOUNT) + """),
  "current_price": number,
  "thesis": "brief thesis",
  "confidence": number 1-10,
  "indicators": "key technical signals",
  "market_condition": "brief market context",
  "stop_loss_pct": number (e.g. 0.08 for 8%),
  "take_profit_pct": number (e.g. 0.15 for 15%),
  "time_limit_min": number (minutes to hold, 0 for no limit)
}

When criteria are NOT met, respond ONLY with:
{
  "action": "NO_TRADE",
  "reason": "explanation of why criteria were not met"
}

Do not include any text outside the JSON."""


def ask_claude_for_trade(session_label: str, market_data: dict) -> dict:
    """Send market data to Claude and get a trade decision back."""
    prompt = f"""
Market session: {session_label}
Current time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S ET")}

MARKET SNAPSHOT:
{json.dumps(market_data, indent=2)}

Analyze this data and decide whether to place a trade.
Remember: only trade if confidence is 7+, momentum is confirmed, AND a news catalyst exists.
"""
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SCHEDULER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        raw  = response.content[0].text.strip()
        # Strip any accidental markdown fences
        raw  = raw.replace("```json","").replace("```","").strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"Claude response parse error: {e} | Raw: {raw[:200]}")
        return {"action": "NO_TRADE", "reason": f"Parse error: {e}"}
    except Exception as e:
        log(f"Claude API error: {e}")
        return {"action": "NO_TRADE", "reason": f"API error: {e}"}


# ─────────────────────────────────────────────
#  ACTIVE MONITORS  (in-memory, scheduler process)
# ─────────────────────────────────────────────
active_monitors = {}


def check_monitors():
    """Called every 60 seconds to check exit conditions on scheduler-placed trades."""
    to_remove = []
    for symbol, rules in active_monitors.items():
        try:
            now         = datetime.now()
            elapsed     = (now - rules["entry_time"]).total_seconds() / 60
            asset_type  = rules["asset_type"]
            entry_price = rules["entry_price"]
            stop_pct    = rules["stop_loss_pct"]
            tp_pct      = rules["take_profit_pct"]
            time_lim    = rules["time_limit_min"]

            if time_lim and elapsed >= time_lim:
                cp = get_current_price(symbol, asset_type) or entry_price
                r  = close_position(symbol, asset_type)
                if r["success"]:
                    db_log_exit(symbol, cp, f"time limit ({time_lim}min)")
                    log(f"AUTO-SOLD {symbol} | time limit | exit ${cp:.4f}")
                else:
                    log(f"AUTO-SELL FAILED {symbol}: {r.get('error')}")
                to_remove.append(symbol)
                continue

            cp = get_current_price(symbol, asset_type)
            if not cp:
                continue
            pct = (cp - entry_price) / entry_price

            if stop_pct and pct <= -stop_pct:
                r = close_position(symbol, asset_type)
                if r["success"]:
                    db_log_exit(symbol, cp, f"stop loss ({pct*100:.2f}%)")
                    log(f"AUTO-SOLD {symbol} | stop loss {pct*100:.2f}% | exit ${cp:.4f}")
                to_remove.append(symbol)
                continue

            if tp_pct and pct >= tp_pct:
                r = close_position(symbol, asset_type)
                if r["success"]:
                    db_log_exit(symbol, cp, f"take profit (+{pct*100:.2f}%)")
                    log(f"AUTO-SOLD {symbol} | take profit +{pct*100:.2f}% | exit ${cp:.4f}")
                to_remove.append(symbol)
                continue

            log(f"Monitoring {symbol} | ${cp:.4f} | {pct*100:.2f}% | {elapsed:.1f}min elapsed")

        except Exception as e:
            log(f"Monitor error on {symbol}: {e}")

    for sym in to_remove:
        active_monitors.pop(sym, None)


# ─────────────────────────────────────────────
#  MAIN TRADING SESSION
# ─────────────────────────────────────────────
def run_trading_session(session_label: str):
    log(f"═══ Starting {session_label} session ═══")

    # 1. Fetch market data
    log("Fetching market snapshot...")
    market_data = fetch_market_snapshot()
    log(f"Snapshot fetched. VIX={market_data.get('VIX','?')} | "
        f"SPY={market_data.get('indices',{}).get('SPY',{}).get('change_pct','?')}%")

    # 2. Ask Claude for a trade decision
    log("Asking Claude for trade analysis...")
    decision = ask_claude_for_trade(session_label, market_data)
    log(f"Claude decision: {decision.get('action')} | "
        f"{decision.get('reason') or decision.get('thesis','')}")

    if decision.get("action") != "TRADE":
        log(f"NO TRADE placed. Reason: {decision.get('reason','unknown')}")
        return

    # 3. Validate decision fields
    required = ["symbol","asset_type","side","dollar_amount","current_price",
                "thesis","confidence","stop_loss_pct","take_profit_pct"]
    if not all(k in decision for k in required):
        log(f"Invalid decision format — missing fields. Skipping.")
        return

    if decision["confidence"] < 7:
        log(f"Confidence {decision['confidence']}/10 is below threshold. Skipping.")
        return

    if decision["dollar_amount"] > MAX_TRADE_AMOUNT:
        log(f"Trade amount ${decision['dollar_amount']} exceeds max ${MAX_TRADE_AMOUNT}. Capping.")
        decision["dollar_amount"] = MAX_TRADE_AMOUNT

    symbol     = decision["symbol"].upper().replace("/USD","")
    asset_type = decision["asset_type"]
    side       = decision["side"]
    amount     = decision["dollar_amount"]
    price      = decision["current_price"]

    # 4. Place the order
    log(f"Placing {side.upper()} order: {symbol} | ${amount} | confidence {decision['confidence']}/10")
    order = place_order(symbol, asset_type, side, amount, price)

    if not order.get("success"):
        log(f"Order FAILED: {order.get('error')}")
        return

    log(f"Order placed ✅ | order_id={order['order_id']} | qty={order['qty']}")

    # 5. Log to database
    db_log_entry(
        symbol=symbol, asset_type=asset_type, side=side,
        entry_price=price, qty=order["qty"], dollar_amount=amount,
        thesis=decision.get("thesis",""), confidence=decision.get("confidence",0),
        indicators=decision.get("indicators",""), market_condition=decision.get("market_condition",""),
        order_id=order["order_id"]
    )

    # 6. Set monitor
    active_monitors[symbol] = {
        "entry_price":     price,
        "entry_time":      datetime.now(),
        "asset_type":      asset_type,
        "stop_loss_pct":   decision.get("stop_loss_pct", 0.10),
        "take_profit_pct": decision.get("take_profit_pct", 0.20),
        "time_limit_min":  decision.get("time_limit_min", 0)
    }
    log(f"Monitor set for {symbol} | "
        f"SL={decision.get('stop_loss_pct',0)*100:.0f}% | "
        f"TP={decision.get('take_profit_pct',0)*100:.0f}% | "
        f"Time={decision.get('time_limit_min',0)}min")

    log(f"═══ {session_label} session complete ═══")


# ─────────────────────────────────────────────
#  SCHEDULE  (all times ET)
# ─────────────────────────────────────────────
def market_open_session():
    run_trading_session("9:31AM Market Open")

def midday_session():
    run_trading_session("12:00PM Midday")

def close_session():
    run_trading_session("3:30PM Pre-Close")


if __name__ == "__main__":
    log("🚀 FinSight Autonomous Scheduler starting...")
    log(f"Max trade amount: ${MAX_TRADE_AMOUNT}")
    log(f"Trading URL: {ALPACA_TRADING_URL}")
    log("Scheduled sessions: 9:31AM | 12:00PM | 3:30PM ET (weekdays only)")

    # Schedule trading sessions
    schedule.every().monday.at("09:31").do(market_open_session)
    schedule.every().tuesday.at("09:31").do(market_open_session)
    schedule.every().wednesday.at("09:31").do(market_open_session)
    schedule.every().thursday.at("09:31").do(market_open_session)
    schedule.every().friday.at("09:31").do(market_open_session)

    schedule.every().monday.at("12:00").do(midday_session)
    schedule.every().tuesday.at("12:00").do(midday_session)
    schedule.every().wednesday.at("12:00").do(midday_session)
    schedule.every().thursday.at("12:00").do(midday_session)
    schedule.every().friday.at("12:00").do(midday_session)

    schedule.every().monday.at("15:30").do(close_session)
    schedule.every().tuesday.at("15:30").do(close_session)
    schedule.every().wednesday.at("15:30").do(close_session)
    schedule.every().thursday.at("15:30").do(close_session)
    schedule.every().friday.at("15:30").do(close_session)

    # Monitor loop — checks open positions every 60 seconds
    schedule.every(60).seconds.do(check_monitors)

    log("Scheduler running. Waiting for next session...")
    while True:
        schedule.run_pending()
        time.sleep(1)
