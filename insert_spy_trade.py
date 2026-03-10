"""
One-time script to insert the SPY 0DTE call trade into the FinSight journal.
Run once on Railway or locally then delete.
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "finsight_trades.db")

conn = sqlite3.connect(DB_PATH)
c    = conn.cursor()

# Create table if it doesn't exist yet
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

c.execute("""
    INSERT INTO trades
    (symbol, asset_type, side, entry_price, exit_price, qty, dollar_amount,
     pnl, pnl_pct, exit_reason, thesis, confidence, indicators, market_condition,
     entry_time, exit_time, time_held_min, order_id, status)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'closed')
""", (
    "SPY",
    "option",
    "buy",
    1.46,
    3.75,
    "1 contract",
    146.0,
    229.0,
    156.8,
    "Manual profit-taking",
    "0DTE power hour momentum, SPY broke $675 into close",
    6,
    "0DTE call, power hour, SPY broke resistance at $675",
    "SPY down -1% on day, recovering into close",
    "2026-03-09 15:24:59",
    "2026-03-09 15:45:00",
    20,
    "manual-entry"
))

conn.commit()
conn.close()
print("✅ SPY trade inserted successfully.")
