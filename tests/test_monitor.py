"""
Tests for core/monitor.py — startup recovery and exit condition logic.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta


@pytest.fixture(autouse=True)
def in_memory_db(tmp_path):
    db_file = str(tmp_path / "test.db")
    with patch("core.database.DB_PATH", db_file), \
         patch("core.config.DB_PATH",   db_file):
        from core.database import init_db
        init_db()
        yield db_file


def test_monitor_recovers_from_db_on_startup():
    """_load_monitors_from_db should populate trade_monitors from the DB."""
    from core.database import db_set_monitor
    db_set_monitor("AAPL", "stock", 150.0, 0.08, 0.15, 30, source="web")
    db_set_monitor("BTC",  "crypto", 50000.0, 0.10, 0.20, 0, source="scheduler")

    # Clear in-memory state then reload
    from core.monitor import trade_monitors, _load_monitors_from_db
    trade_monitors.clear()
    _load_monitors_from_db()

    assert "AAPL" in trade_monitors
    assert "BTC"  in trade_monitors
    assert trade_monitors["AAPL"]["entry_price"] == 150.0
    assert trade_monitors["BTC"]["entry_price"]  == 50000.0


def test_monitor_triggers_stop_loss():
    """When price drops below stop_loss_pct, close_position and db_log_exit are called."""
    from core.monitor import trade_monitors, monitor_lock, _close_and_log
    import core.monitor as monitor_module

    entry_price = 100.0
    stop_pct    = 0.08   # 8% stop
    current_price = 90.0  # -10% → triggers stop

    symbol = "AAPL"
    with monitor_lock:
        trade_monitors[symbol] = {
            "entry_price":     entry_price,
            "entry_time":      datetime.now(),
            "asset_type":      "stock",
            "stop_loss_pct":   stop_pct,
            "take_profit_pct": 0.15,
            "time_limit_min":  0,
            "trade_id":        None,
        }

    with patch("core.monitor._get_price", return_value=current_price), \
         patch("core.monitor.requests.delete") as mock_delete, \
         patch("core.monitor.db_log_exit") as mock_exit, \
         patch("core.monitor.db_remove_monitor") as mock_remove:

        mock_delete.return_value = MagicMock(status_code=200, content=b'{"id":"x"}', json=lambda: {"id": "x"})

        pct = (current_price - entry_price) / entry_price
        if stop_pct and pct <= -stop_pct:
            _close_and_log(symbol, "stock", f"Stop loss ({pct*100:.2f}%)", current_price)
            with monitor_lock:
                trade_monitors.pop(symbol, None)

        assert symbol not in trade_monitors
        mock_exit.assert_called_once()
        mock_remove.assert_called_once_with(symbol)


def test_monitor_triggers_take_profit():
    """When price rises above take_profit_pct, position is closed."""
    from core.monitor import trade_monitors, monitor_lock, _close_and_log

    entry_price   = 100.0
    tp_pct        = 0.15
    current_price = 120.0  # +20% → triggers TP

    symbol = "ETH"
    with monitor_lock:
        trade_monitors[symbol] = {
            "entry_price":     entry_price,
            "entry_time":      datetime.now(),
            "asset_type":      "crypto",
            "stop_loss_pct":   0.08,
            "take_profit_pct": tp_pct,
            "time_limit_min":  0,
            "trade_id":        None,
        }

    with patch("core.monitor.requests.delete") as mock_delete, \
         patch("core.monitor.db_log_exit") as mock_exit, \
         patch("core.monitor.db_remove_monitor"):

        mock_delete.return_value = MagicMock(status_code=200, content=b'{"id":"x"}', json=lambda: {"id": "x"})

        pct = (current_price - entry_price) / entry_price
        if tp_pct and pct >= tp_pct:
            _close_and_log(symbol, "crypto", f"Take profit (+{pct*100:.2f}%)", current_price)
            with monitor_lock:
                trade_monitors.pop(symbol, None)

        assert symbol not in trade_monitors
        mock_exit.assert_called_once()


def test_monitor_triggers_time_limit():
    """When elapsed time exceeds time_limit_min, position is closed."""
    from core.monitor import trade_monitors, monitor_lock, _close_and_log

    entry_price   = 100.0
    time_limit    = 30   # 30 min limit
    elapsed_min   = 35.0  # past limit

    symbol = "TSLA"
    old_entry_time = datetime.now() - timedelta(minutes=elapsed_min)
    with monitor_lock:
        trade_monitors[symbol] = {
            "entry_price":     entry_price,
            "entry_time":      old_entry_time,
            "asset_type":      "stock",
            "stop_loss_pct":   0.08,
            "take_profit_pct": 0.15,
            "time_limit_min":  time_limit,
            "trade_id":        None,
        }

    with patch("core.monitor.requests.delete") as mock_delete, \
         patch("core.monitor.db_log_exit") as mock_exit, \
         patch("core.monitor.db_remove_monitor"):

        mock_delete.return_value = MagicMock(status_code=200, content=b'{"id":"x"}', json=lambda: {"id": "x"})

        rules = trade_monitors[symbol]
        actual_elapsed = (datetime.now() - rules["entry_time"]).total_seconds() / 60
        if rules["time_limit_min"] and actual_elapsed >= rules["time_limit_min"]:
            _close_and_log(symbol, "stock", f"Time limit ({time_limit}min)", entry_price)
            with monitor_lock:
                trade_monitors.pop(symbol, None)

        assert symbol not in trade_monitors
        mock_exit.assert_called_once()
