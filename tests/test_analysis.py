"""
Tests for core/analysis.py (pure logic, no real API calls).
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime


# ── get_trading_session ───────────────────────────────────────

def _session_at(hour, minute):
    """Patch datetime.now() to return a specific time and call get_trading_session."""
    from core.analysis import get_trading_session
    fake_now = datetime(2026, 3, 26, hour, minute, 0)
    with patch("core.analysis.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromtimestamp = datetime.fromtimestamp  # keep this intact
        return get_trading_session()


def test_session_market_closed_before_open():
    result = _session_at(9, 0)
    assert result["session"] == "closed"
    assert result["quality"] == "none"
    assert result["recommendation"] == "avoid"


def test_session_market_closed_after_close():
    result = _session_at(16, 30)
    assert result["session"] == "closed"


def test_session_open_chop():
    result = _session_at(9, 35)
    assert result["session"] == "open_chop"
    assert result["quality"] == "poor"


def test_session_prime_window():
    result = _session_at(10, 30)
    assert result["session"] == "prime"
    assert result["quality"] == "best"
    assert result["warning"] is None


def test_session_lunch_chop():
    result = _session_at(12, 0)
    assert result["session"] == "lunch_chop"
    assert result["quality"] == "poor"


def test_session_power_hour():
    result = _session_at(15, 15)
    assert result["session"] == "power_hour"
    assert result["quality"] == "good"


def test_session_late():
    result = _session_at(15, 45)
    assert result["session"] == "late"
    assert result["quality"] == "poor"


def test_session_exactly_at_open():
    result = _session_at(9, 30)
    # 9:30 is exactly OPEN — should be open_chop (first 15 min)
    assert result["session"] == "open_chop"


# ── Bayesian probability (via prediction module) ──────────────

def test_bayesian_base_rate_no_evidence():
    from core.prediction import calculate_bayesian_probability
    result = calculate_bayesian_probability(0.5, [])
    assert abs(result["posterior"] - 0.5) < 0.001
    assert result["confidence"] == "low"


def test_bayesian_strong_supporting_evidence():
    from core.prediction import calculate_bayesian_probability
    result = calculate_bayesian_probability(0.5, [
        {"description": "Strong bullish signal", "likelihood_ratio": 3.0},
        {"description": "News catalyst",         "likelihood_ratio": 2.0},
        {"description": "Volume spike",          "likelihood_ratio": 2.0},
    ])
    assert result["posterior"] > 0.9
    assert result["confidence"] == "high"


def test_bayesian_strong_opposing_evidence():
    from core.prediction import calculate_bayesian_probability
    result = calculate_bayesian_probability(0.8, [
        {"description": "Bearish divergence", "likelihood_ratio": 0.1},
        {"description": "Bad news",           "likelihood_ratio": 0.2},
    ])
    assert result["posterior"] < 0.5


def test_bayesian_neutral_evidence_no_change():
    from core.prediction import calculate_bayesian_probability
    result = calculate_bayesian_probability(0.6, [
        {"description": "Neutral", "likelihood_ratio": 1.0},
    ])
    assert abs(result["posterior"] - 0.6) < 0.001


# ── Kelly Criterion ───────────────────────────────────────────

def test_kelly_no_edge():
    from core.prediction import calculate_kelly_criterion
    # Our prob == market prob → no edge → kelly = 0
    result = calculate_kelly_criterion(our_probability=0.55, market_yes_price=55)
    assert result["kelly_fraction"] == "0.0%"
    assert result["recommended_bet"] == "$0.00"


def test_kelly_strong_edge():
    from core.prediction import calculate_kelly_criterion
    # We think 80% but market says 50% → strong edge
    result = calculate_kelly_criterion(our_probability=0.80, market_yes_price=50, bankroll=1000)
    fraction_str = result["kelly_fraction"]
    fraction     = float(fraction_str.replace("%", "")) / 100
    assert fraction > 0
    assert "strong edge" in result["signal"]


def test_kelly_capped_at_max_fraction():
    from core.prediction import calculate_kelly_criterion
    # Extreme edge — should be capped at 25%
    result = calculate_kelly_criterion(our_probability=0.99, market_yes_price=1, max_fraction=0.25)
    fraction = float(result["kelly_fraction"].replace("%", "")) / 100
    assert fraction <= 0.25


def test_kelly_bets_no_side():
    from core.prediction import calculate_kelly_criterion
    result = calculate_kelly_criterion(our_probability=0.3, market_yes_price=55)
    assert result["bet_side"] == "NO"


def test_kelly_bets_yes_side():
    from core.prediction import calculate_kelly_criterion
    result = calculate_kelly_criterion(our_probability=0.7, market_yes_price=55)
    assert result["bet_side"] == "YES"
