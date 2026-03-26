"""
Autonomous position monitor thread.
Runs in the web process (when RUN_MONITOR_IN_WEB=true) or always in the scheduler.
Recovers persisted monitors from the database on startup.
"""
import time
import threading
import requests
from datetime import datetime
from core.config import ALPACA_TRADING_URL, ALPACA_DATA_URL, ALPACA_CRYPTO_URL, ALPACA_HEADERS, CRYPTO_SYMBOLS
from core.database import db_log_exit, db_get_active_monitors, db_remove_monitor

trade_monitors: dict = {}
monitor_lock         = threading.Lock()
monitor_log: list    = []


def _get_price(symbol: str, asset_type: str):
    try:
        if asset_type == "crypto":
            pair = f"{symbol}/USD"
            s    = requests.get(f"{ALPACA_CRYPTO_URL}/snapshots", headers=ALPACA_HEADERS,
                                params={"symbols": pair}, timeout=8).json()
            snap = s.get("snapshots", {}).get(pair, {})
            return snap.get("latestTrade", {}).get("p") or snap.get("dailyBar", {}).get("c")
        else:
            snap = requests.get(f"{ALPACA_DATA_URL}/stocks/{symbol}/snapshot",
                                headers=ALPACA_HEADERS, timeout=8).json()
            return snap.get("latestTrade", {}).get("p") or snap.get("dailyBar", {}).get("c")
    except Exception:
        return None


def _close_and_log(symbol: str, asset_type: str, reason: str, exit_price: float, trade_id=None):
    api_symbol = f"{symbol}USD" if asset_type == "crypto" else symbol
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
            db_log_exit(symbol, exit_price, reason, trade_id=trade_id)
            db_remove_monitor(symbol)
        msg = f"[{datetime.now().strftime('%H:%M:%S')}] AUTO-SELL {symbol} | {reason} | {'✅ Done' if success else '❌ Failed'}"
    except Exception as e:
        msg = f"[{datetime.now().strftime('%H:%M:%S')}] AUTO-SELL {symbol} FAILED | {e}"
    with monitor_lock:
        monitor_log.append(msg)
    print(f"[FinSight Monitor] {msg}")


def _load_monitors_from_db():
    """Rehydrate in-memory dict from the monitors table on startup."""
    for row in db_get_active_monitors():
        symbol = row["symbol"]
        try:
            entry_time = datetime.fromisoformat(row["entry_time"])
        except Exception:
            entry_time = datetime.now()
        trade_monitors[symbol] = {
            "entry_price":     row["entry_price"],
            "entry_time":      entry_time,
            "asset_type":      row["asset_type"],
            "stop_loss_pct":   row["stop_loss_pct"],
            "take_profit_pct": row["take_profit_pct"],
            "time_limit_min":  row["time_limit_min"],
            "trade_id":        row.get("trade_id"),
        }
    if trade_monitors:
        print(f"[FinSight Monitor] Recovered {len(trade_monitors)} monitor(s) from DB: {list(trade_monitors.keys())}")


def run_trade_monitor():
    print("[FinSight Monitor] 🟢 Autonomous trade monitor started")
    _load_monitors_from_db()

    while True:
        time.sleep(60)
        with monitor_lock:
            symbols_to_check = dict(trade_monitors)

        for symbol, rules in symbols_to_check.items():
            try:
                now         = datetime.now()
                entry_price = rules["entry_price"]
                entry_time  = rules["entry_time"]
                asset_type  = rules["asset_type"]
                stop_pct    = rules["stop_loss_pct"]
                tp_pct      = rules["take_profit_pct"]
                time_lim    = rules["time_limit_min"]
                trade_id    = rules.get("trade_id")
                elapsed_min = (now - entry_time).total_seconds() / 60

                if time_lim and elapsed_min >= time_lim:
                    cp = _get_price(symbol, asset_type) or 0
                    _close_and_log(symbol, asset_type, f"Time limit ({time_lim}min)", cp, trade_id)
                    with monitor_lock:
                        trade_monitors.pop(symbol, None)
                    continue

                cp = _get_price(symbol, asset_type)
                if not cp:
                    continue
                pct = (cp - entry_price) / entry_price

                if stop_pct and pct <= -stop_pct:
                    _close_and_log(symbol, asset_type, f"Stop loss ({pct*100:.2f}%)", cp, trade_id)
                    with monitor_lock:
                        trade_monitors.pop(symbol, None)
                    continue

                if tp_pct and pct >= tp_pct:
                    _close_and_log(symbol, asset_type, f"Take profit (+{pct*100:.2f}%)", cp, trade_id)
                    with monitor_lock:
                        trade_monitors.pop(symbol, None)
                    continue

                msg = f"[{now.strftime('%H:%M:%S')}] {symbol} | ${cp:.4f} | {pct*100:.2f}% | {elapsed_min:.1f}min elapsed"
                with monitor_lock:
                    monitor_log.append(msg)
                print(f"[FinSight Monitor] {msg}")

            except Exception as e:
                print(f"[FinSight Monitor] Error on {symbol}: {e}")
