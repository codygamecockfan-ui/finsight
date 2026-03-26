"""
FinSight Autonomous Trading Scheduler
Runs at 9:31AM, 12:00PM, and 3:30PM ET on weekdays.
Only places trades when ALL criteria are met (defined in agent/prompts.py TRADE_CRITERIA).
Max trade size controlled by MAX_TRADE_AMOUNT env var (default $500).
To go live: change ALPACA_TRADING_URL in core/config.py to https://api.alpaca.markets/v2
"""
import json
import time
import schedule
import threading
from datetime import datetime
from anthropic import Anthropic

from core.config import MAX_TRADE_AMOUNT, TRADE_CONFIDENCE_THRESHOLD
from core.database import init_db, db_log_entry, db_has_open_position
from core.execution import place_order, get_current_price
from core.market_data import fetch_market_snapshot
from core.monitor import trade_monitors, run_trade_monitor, monitor_lock
from core.database import db_set_monitor
from agent.prompts import SCHEDULER_SYSTEM_PROMPT

client = Anthropic()
init_db()


# ── Logging ──────────────────────────────────────────────────

def log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[Scheduler] [{ts}] {msg}"
    print(line, flush=True)
    try:
        with open("scheduler.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Claude decision ──────────────────────────────────────────

def ask_claude_for_trade(session_label: str, market_data: dict) -> dict:
    prompt = (
        f"Market session: {session_label}\n"
        f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}\n\n"
        f"MARKET SNAPSHOT:\n{json.dumps(market_data, indent=2)}\n\n"
        "Analyze this data and decide whether to place a trade.\n"
        f"Remember: only trade if confidence is {TRADE_CONFIDENCE_THRESHOLD}+, "
        "momentum is confirmed, AND a news catalyst exists."
    )
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=SCHEDULER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"Claude response parse error: {e}")
        return {"action": "NO_TRADE", "reason": f"Parse error: {e}"}
    except Exception as e:
        log(f"Claude API error: {e}")
        return {"action": "NO_TRADE", "reason": f"API error: {e}"}


# ── Trading session ──────────────────────────────────────────

def run_trading_session(session_label: str):
    log(f"═══ Starting {session_label} session ═══")

    log("Fetching market snapshot...")
    market_data = fetch_market_snapshot()
    log(f"Snapshot fetched. VIX={market_data.get('VIX', '?')} | "
        f"SPY={market_data.get('indices', {}).get('SPY', {}).get('change_pct', '?')}%")

    log("Asking Claude for trade analysis...")
    decision = ask_claude_for_trade(session_label, market_data)
    log(f"Claude decision: {decision.get('action')} | "
        f"{decision.get('reason') or decision.get('thesis', '')}")

    if decision.get("action") != "TRADE":
        log(f"NO TRADE placed. Reason: {decision.get('reason', 'unknown')}")
        return

    required = ["symbol", "asset_type", "side", "dollar_amount", "current_price",
                "thesis", "confidence", "stop_loss_pct", "take_profit_pct"]
    if not all(k in decision for k in required):
        log("Invalid decision format — missing fields. Skipping.")
        return

    if decision["confidence"] < TRADE_CONFIDENCE_THRESHOLD:
        log(f"Confidence {decision['confidence']}/10 below threshold {TRADE_CONFIDENCE_THRESHOLD}. Skipping.")
        return

    if decision["dollar_amount"] > MAX_TRADE_AMOUNT:
        log(f"Trade amount ${decision['dollar_amount']} exceeds max ${MAX_TRADE_AMOUNT}. Capping.")
        decision["dollar_amount"] = MAX_TRADE_AMOUNT

    symbol     = decision["symbol"].upper().replace("/USD", "")
    asset_type = decision["asset_type"]
    side       = decision["side"]
    amount     = decision["dollar_amount"]
    price      = decision["current_price"]

    # Prevent duplicate position (cross-process check)
    if db_has_open_position(symbol):
        log(f"Open position already exists for {symbol}. Skipping to avoid duplicate.")
        return

    log(f"Placing {side.upper()} order: {symbol} | ${amount} | confidence {decision['confidence']}/10")
    order = place_order(symbol, asset_type, side, amount, price)

    if not order.get("success"):
        log(f"Order FAILED: {order.get('error')}")
        return

    log(f"Order placed ✅ | order_id={order['order_id']} | qty={order['qty']}")

    trade_id = db_log_entry(
        symbol=symbol, asset_type=asset_type, side=side,
        entry_price=price, qty=order["qty"], dollar_amount=amount,
        thesis=decision.get("thesis", ""), confidence=decision.get("confidence", 0),
        indicators=decision.get("indicators", ""), market_condition=decision.get("market_condition", ""),
        order_id=order["order_id"]
    )

    sl_pct  = decision.get("stop_loss_pct", 0.10)
    tp_pct  = decision.get("take_profit_pct", 0.20)
    tl_min  = decision.get("time_limit_min", 0)

    inserted = db_set_monitor(
        symbol=symbol, asset_type=asset_type, entry_price=price,
        stop_loss_pct=sl_pct, take_profit_pct=tp_pct,
        time_limit_min=tl_min, source="scheduler", trade_id=trade_id
    )
    if inserted:
        with monitor_lock:
            trade_monitors[symbol] = {
                "entry_price":     price,
                "entry_time":      datetime.now(),
                "asset_type":      asset_type,
                "stop_loss_pct":   sl_pct,
                "take_profit_pct": tp_pct,
                "time_limit_min":  tl_min,
                "trade_id":        trade_id,
            }
        log(f"Monitor set for {symbol} | SL={sl_pct*100:.0f}% | TP={tp_pct*100:.0f}% | Time={tl_min}min")
    else:
        log(f"Monitor already active for {symbol} — not overwriting.")

    log(f"═══ {session_label} session complete ═══")


# ── Scheduled sessions ────────────────────────────────────────

def market_open_session(): run_trading_session("9:31AM Market Open")
def midday_session():      run_trading_session("12:00PM Midday")
def close_session():       run_trading_session("3:30PM Pre-Close")


if __name__ == "__main__":
    log("🚀 FinSight Autonomous Scheduler starting...")
    log(f"Max trade amount: ${MAX_TRADE_AMOUNT}")
    log(f"Confidence threshold: {TRADE_CONFIDENCE_THRESHOLD}/10")
    log("Scheduled sessions: 9:31AM | 12:00PM | 3:30PM ET (weekdays only)")

    # Start persistent monitor thread
    monitor_thread = threading.Thread(target=run_trade_monitor, daemon=True)
    monitor_thread.start()

    for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        getattr(schedule.every(), day).at("09:31").do(market_open_session)
        getattr(schedule.every(), day).at("12:00").do(midday_session)
        getattr(schedule.every(), day).at("15:30").do(close_session)

    log("Scheduler running. Waiting for next session...")
    while True:
        schedule.run_pending()
        time.sleep(1)
