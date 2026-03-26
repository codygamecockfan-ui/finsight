"""
Order execution via Alpaca paper trading API.
Shared between app.py (manual trades) and scheduler.py (autonomous trades).
"""
import requests
from core.config import (
    ALPACA_TRADING_URL, ALPACA_DATA_URL, ALPACA_CRYPTO_URL,
    ALPACA_HEADERS, CRYPTO_SYMBOLS
)


def place_order(symbol: str, asset_type: str, side: str,
                dollar_amount: float, current_price: float) -> dict:
    symbol = symbol.upper().replace("/USD", "")
    try:
        if asset_type == "crypto":
            order_data = {
                "symbol": f"{symbol}/USD",
                "notional": str(round(dollar_amount, 2)),
                "side": side, "type": "market", "time_in_force": "ioc",
            }
            qty_display = f"~{round(dollar_amount / current_price, 6)} {symbol}"
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
        return {"success": False, "error": result.get("message", "Order failed")}
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
        return {"success": False, "error": result.get("message", "Failed")}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_current_price(symbol: str, asset_type: str) -> float | None:
    try:
        if asset_type == "crypto":
            pair = f"{symbol}/USD"
            s    = requests.get(f"{ALPACA_CRYPTO_URL}/snapshots", headers=ALPACA_HEADERS,
                                params={"symbols": pair}, timeout=8).json()
            snap = s.get("snapshots", {}).get(pair, {})
            return snap.get("latestTrade", {}).get("p") or snap.get("dailyBar", {}).get("c")
        else:
            s = requests.get(f"{ALPACA_DATA_URL}/stocks/{symbol}/snapshot",
                             headers=ALPACA_HEADERS, timeout=8).json()
            return s.get("latestTrade", {}).get("p") or s.get("dailyBar", {}).get("c")
    except Exception:
        return None
