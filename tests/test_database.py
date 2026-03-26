"""
Tests for core/database.py.
Uses an in-memory SQLite DB — no file I/O, no external deps.
"""
import pytest
import sqlite3
from unittest.mock import patch
from datetime import datetime


# ── Fixture: in-memory DB ─────────────────────────────────────

@pytest.fixture(autouse=True)
def in_memory_db(tmp_path):
    """
    Redirect DB_PATH to an in-memory path for each test.
    We use a temp file (not :memory:) so multiple connections can share it.
    """
    db_file = str(tmp_path / "test.db")
    with patch("core.database.DB_PATH", db_file), \
         patch("core.config.DB_PATH",   db_file):
        from core.database import init_db
        init_db()
        yield db_file


# ── Helpers ───────────────────────────────────────────────────

def _fresh():
    """Re-import functions so they pick up the patched DB_PATH."""
    from core.database import (
        db_log_entry, db_log_exit, db_has_open_position,
        db_get_all_trades, db_get_performance_summary,
        db_set_monitor, db_get_active_monitors, db_remove_monitor, db_get_monitor,
    )
    return (db_log_entry, db_log_exit, db_has_open_position,
            db_get_all_trades, db_get_performance_summary,
            db_set_monitor, db_get_active_monitors, db_remove_monitor, db_get_monitor)


# ── Trade journal tests ───────────────────────────────────────

def test_db_log_entry_creates_open_trade():
    db_log_entry, *_ = _fresh()
    trade_id = db_log_entry("AAPL", "stock", "buy", 150.0, "1", 150.0,
                            thesis="test", confidence=8)
    assert isinstance(trade_id, int)
    assert trade_id > 0

    from core.database import db_get_all_trades
    trades = db_get_all_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t["symbol"] == "AAPL"
    assert t["status"] == "open"
    assert t["entry_price"] == 150.0
    assert t["confidence"] == 8


def test_db_log_exit_calculates_pnl():
    db_log_entry, db_log_exit, *_ = _fresh()
    db_log_entry("AAPL", "stock", "buy", 100.0, "1", 100.0)
    db_log_exit("AAPL", 120.0, "take profit")

    from core.database import db_get_all_trades
    trades = db_get_all_trades()
    t = trades[0]
    assert t["status"] == "closed"
    assert t["exit_price"] == 120.0
    assert t["exit_reason"] == "take profit"
    assert abs(t["pnl_pct"] - 20.0) < 0.01     # 20% gain
    assert abs(t["pnl"] - 20.0) < 0.01          # $20 on $100 investment


def test_db_log_exit_loss_calculates_negative_pnl():
    db_log_entry, db_log_exit, *_ = _fresh()
    db_log_entry("BTC", "crypto", "buy", 50000.0, "0.002", 100.0)
    db_log_exit("BTC", 40000.0, "stop loss")

    from core.database import db_get_all_trades
    t = db_get_all_trades()[0]
    assert t["pnl_pct"] < 0
    assert t["pnl"] < 0


def test_db_log_exit_no_open_entry_does_not_crash():
    """Exiting a symbol with no logged entry should not raise."""
    _, db_log_exit, *_ = _fresh()
    db_log_exit("TSLA", 300.0, "manual close")  # should not raise

    from core.database import db_get_all_trades
    trades = db_get_all_trades()
    # A fallback row is inserted with status='closed'
    assert len(trades) == 1
    assert trades[0]["status"] == "closed"


def test_db_log_exit_targets_specific_trade_id():
    db_log_entry, db_log_exit, *_ = _fresh()
    t1 = db_log_entry("AAPL", "stock", "buy", 100.0, "1", 100.0)
    t2 = db_log_entry("AAPL", "stock", "buy", 110.0, "1", 110.0)
    db_log_exit("AAPL", 130.0, "take profit", trade_id=t1)

    from core.database import db_get_all_trades
    trades = {t["id"]: t for t in db_get_all_trades()}
    assert trades[t1]["status"] == "closed"
    assert trades[t2]["status"] == "open"


def test_db_has_open_position_true_and_false():
    db_log_entry, db_log_exit, db_has_open_position, *_ = _fresh()
    assert not db_has_open_position("AAPL")
    db_log_entry("AAPL", "stock", "buy", 100.0, "1", 100.0)
    assert db_has_open_position("AAPL")
    db_log_exit("AAPL", 110.0, "manual close")
    assert not db_has_open_position("AAPL")


def test_db_get_performance_summary_empty_db():
    *_, db_get_performance_summary, _ = _fresh()
    from core.database import db_get_performance_summary
    summary = db_get_performance_summary()
    assert summary["total_trades"] == 0
    assert summary["win_rate_pct"] == 0
    assert summary["total_pnl"] == 0


def test_db_get_performance_summary_win_rate():
    db_log_entry, db_log_exit, *_ = _fresh()
    from core.database import db_get_performance_summary
    # 2 winners, 1 loser
    for price, exit_p in [(100, 120), (100, 115), (100, 80)]:
        db_log_entry("SPY", "stock", "buy", price, "1", price)
        db_log_exit("SPY", exit_p, "manual")

    summary = db_get_performance_summary()
    assert summary["closed_trades"] == 3
    assert summary["winners"] == 2
    assert summary["losers"] == 1
    assert abs(summary["win_rate_pct"] - 66.7) < 0.2


# ── Monitor tests ─────────────────────────────────────────────

def test_db_set_monitor_persists_and_retrieves():
    *_, db_set_monitor, db_get_active_monitors, _, db_get_monitor = _fresh()
    result = db_set_monitor("AAPL", "stock", 150.0, 0.08, 0.15, 30)
    assert result is True

    monitors = db_get_active_monitors()
    assert len(monitors) == 1
    m = monitors[0]
    assert m["symbol"] == "AAPL"
    assert m["entry_price"] == 150.0
    assert m["stop_loss_pct"] == 0.08
    assert m["take_profit_pct"] == 0.15
    assert m["time_limit_min"] == 30


def test_db_set_monitor_returns_false_on_duplicate():
    *_, db_set_monitor, db_get_active_monitors, _, _ = _fresh()
    first  = db_set_monitor("AAPL", "stock", 150.0, 0.08, 0.15, 30)
    second = db_set_monitor("AAPL", "stock", 160.0, 0.10, 0.20, 60)
    assert first is True
    assert second is False
    # Original monitor unchanged
    monitors = db_get_active_monitors()
    assert len(monitors) == 1
    assert monitors[0]["entry_price"] == 150.0


def test_db_remove_monitor_clears_row():
    *_, db_set_monitor, db_get_active_monitors, db_remove_monitor, _ = _fresh()
    db_set_monitor("BTC", "crypto", 50000.0, 0.10, 0.20, 0)
    assert len(db_get_active_monitors()) == 1
    db_remove_monitor("BTC")
    assert len(db_get_active_monitors()) == 0


def test_db_get_active_monitors_returns_all():
    *_, db_set_monitor, db_get_active_monitors, _, _ = _fresh()
    db_set_monitor("AAPL", "stock",  150.0, 0.08, 0.15, 30)
    db_set_monitor("BTC",  "crypto", 50000.0, 0.10, 0.20, 0)
    db_set_monitor("ETH",  "crypto", 3000.0, 0.10, 0.20, 0)
    monitors = db_get_active_monitors()
    assert len(monitors) == 3
    symbols = {m["symbol"] for m in monitors}
    assert symbols == {"AAPL", "BTC", "ETH"}
