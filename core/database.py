"""
Single source of truth for all SQLite operations.
Both app.py and scheduler.py import from here.
Uses WAL mode to allow safe concurrent reads from both processes.
"""
import sqlite3
from datetime import datetime
from core.config import DB_PATH


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    if DB_PATH and DB_PATH != ":memory:":
        import os
        parent = os.path.dirname(DB_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol           TEXT    NOT NULL,
            asset_type       TEXT    NOT NULL,
            side             TEXT    NOT NULL,
            entry_price      REAL    NOT NULL,
            exit_price       REAL,
            qty              TEXT    NOT NULL,
            dollar_amount    REAL    NOT NULL,
            pnl              REAL,
            pnl_pct          REAL,
            exit_reason      TEXT,
            thesis           TEXT,
            confidence       INTEGER,
            indicators       TEXT,
            market_condition TEXT,
            entry_time       TEXT    NOT NULL,
            exit_time        TEXT,
            time_held_min    REAL,
            order_id         TEXT,
            status           TEXT    DEFAULT 'open'
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS monitors (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol           TEXT    NOT NULL UNIQUE,
            asset_type       TEXT    NOT NULL,
            entry_price      REAL    NOT NULL,
            entry_time       TEXT    NOT NULL,
            stop_loss_pct    REAL    NOT NULL DEFAULT 0,
            take_profit_pct  REAL    NOT NULL DEFAULT 0,
            time_limit_min   INTEGER NOT NULL DEFAULT 0,
            source           TEXT    NOT NULL DEFAULT 'web',
            trade_id         INTEGER REFERENCES trades(id),
            created_at       TEXT    NOT NULL
        )
    """)
    conn.commit()
    conn.close()


# ── Trade journal ────────────────────────────────────────────

def db_log_entry(symbol, asset_type, side, entry_price, qty, dollar_amount,
                 thesis="", confidence=0, indicators="", market_condition="", order_id="") -> int:
    conn = _connect()
    c = conn.cursor()
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


def db_log_exit(symbol, exit_price, exit_reason, trade_id=None):
    conn = _connect()
    c = conn.cursor()
    if trade_id:
        c.execute("""
            SELECT id, entry_price, dollar_amount, entry_time
            FROM trades WHERE id=? AND status='open'
        """, (trade_id,))
    else:
        c.execute("""
            SELECT id, entry_price, dollar_amount, entry_time
            FROM trades WHERE symbol=? AND status='open'
            ORDER BY id DESC LIMIT 1
        """, (symbol,))
    row = c.fetchone()
    if row:
        tid, entry_price_db, dollar_amount, entry_time_str = row
        exit_time  = datetime.now()
        entry_time = datetime.fromisoformat(entry_time_str)
        time_held  = (exit_time - entry_time).total_seconds() / 60
        pnl_pct    = ((exit_price - entry_price_db) / entry_price_db) * 100 if entry_price_db else 0
        pnl        = dollar_amount * (pnl_pct / 100)
        c.execute("""
            UPDATE trades SET
                exit_price=?, exit_time=?, time_held_min=?,
                pnl=?, pnl_pct=?, exit_reason=?, status='closed'
            WHERE id=?
        """, (exit_price, exit_time.isoformat(), round(time_held, 2),
              round(pnl, 2), round(pnl_pct, 2), exit_reason, tid))
    else:
        now = datetime.now().isoformat()
        c.execute("""
            INSERT INTO trades
            (symbol, asset_type, side, entry_price, exit_price, qty, dollar_amount,
             pnl, pnl_pct, exit_reason, thesis, confidence, indicators, market_condition,
             entry_time, exit_time, time_held_min, order_id, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'closed')
        """, (symbol, 'unknown', 'buy', exit_price, exit_price, '?', 0,
              0, 0, exit_reason, 'Entry not logged - recorded at close',
              0, '', '', now, now, 0, ''))
    conn.commit()
    conn.close()


def db_has_open_position(symbol: str) -> bool:
    """Returns True if an open trade exists for this symbol."""
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM trades WHERE symbol=? AND status='open'", (symbol,))
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def db_get_all_trades() -> list:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT * FROM trades ORDER BY id DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def db_get_performance_summary() -> dict:
    conn = _connect()
    c = conn.cursor()
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

    c.execute("""
        SELECT asset_type,
               COUNT(*) as count,
               SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins,
               AVG(pnl_pct) as avg_pnl_pct,
               SUM(pnl) as total_pnl
        FROM trades WHERE status='closed'
        GROUP BY asset_type
    """)
    by_type = [dict(r) for r in c.fetchall()]

    c.execute("""
        SELECT exit_reason, COUNT(*) as count, AVG(pnl_pct) as avg_pnl_pct
        FROM trades WHERE status='closed'
        GROUP BY exit_reason
    """)
    by_exit = [dict(r) for r in c.fetchall()]
    conn.close()

    closed  = row["closed_trades"] or 0
    winners = row["winners"] or 0
    win_rate = round((winners / closed * 100), 1) if closed > 0 else 0

    return {
        "total_trades":      row["total_trades"] or 0,
        "closed_trades":     closed,
        "open_trades":       row["open_trades"] or 0,
        "win_rate_pct":      win_rate,
        "winners":           winners,
        "losers":            row["losers"] or 0,
        "total_pnl":         round(row["total_pnl"] or 0, 2),
        "avg_pnl_pct":       round(row["avg_pnl_pct"] or 0, 2),
        "avg_time_held_min": round(row["avg_time_held_min"] or 0, 1),
        "best_trade_pnl":    round(row["best_trade"] or 0, 2),
        "worst_trade_pnl":   round(row["worst_trade"] or 0, 2),
        "by_asset_type":     by_type,
        "by_exit_reason":    by_exit,
    }


# ── Position monitors ────────────────────────────────────────

def db_set_monitor(symbol, asset_type, entry_price, stop_loss_pct,
                   take_profit_pct, time_limit_min, source="web", trade_id=None) -> bool:
    """
    Persist a position monitor. Returns False if a monitor already exists
    for this symbol (duplicate prevention across processes).
    """
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        INSERT OR IGNORE INTO monitors
        (symbol, asset_type, entry_price, entry_time, stop_loss_pct,
         take_profit_pct, time_limit_min, source, trade_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (symbol, asset_type, entry_price, datetime.now().isoformat(),
          stop_loss_pct, take_profit_pct, time_limit_min, source,
          trade_id, datetime.now().isoformat()))
    inserted = c.rowcount > 0
    conn.commit()
    conn.close()
    return inserted


def db_get_active_monitors() -> list:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT * FROM monitors")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def db_get_monitor(symbol: str) -> dict | None:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT * FROM monitors WHERE symbol=?", (symbol,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def db_remove_monitor(symbol: str):
    conn = _connect()
    conn.execute("DELETE FROM monitors WHERE symbol=?", (symbol,))
    conn.commit()
    conn.close()
