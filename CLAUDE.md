# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the web app (port 8080 by default)
python app.py

# Run the autonomous scheduler (separate process)
python scheduler.py

# Run tests
pytest tests/ -v

# Run tests with coverage
pytest tests/ -v --cov=core --cov=agent --cov-report=term-missing

# Run a single test file
pytest tests/test_database.py -v
```

## Required Environment Variables

Create a `.env` file (never committed):

```bash
# Required
ANTHROPIC_API_KEY=
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
POLYGON_API_KEY=
NEWS_API_KEY=

# Auth — use ACCESS_TOKENS (preferred) or legacy APP_PASSWORD
ACCESS_TOKENS=my-laptop-token,phone-token  # comma-separated named tokens
APP_PASSWORD=                               # legacy fallback

# Optional with defaults
FLASK_SECRET_KEY=              # auto-generated if missing
AUTO_EXECUTE_THRESHOLD=7       # maps to TRADE_CONFIDENCE_THRESHOLD (1-10)
AUTO_EXECUTE_MAX_DOLLAR=200    # max trade size from manual UI
MAX_TRADE_AMOUNT=500           # max trade size from scheduler
MAX_OPTIONS_PREMIUM=0.80       # max options premium cost

# Optional prediction market APIs
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY=

# Optional
DB_PATH=                       # defaults to finsight_trades.db alongside app.py
TRADIER_API_KEY=               # required for options chain + expected move
RUN_MONITOR_IN_WEB=true        # set to false if scheduler handles monitoring exclusively
```

## Module Structure

```
finsight/
├── app.py              # Flask factory + routes only (~100 lines)
├── scheduler.py        # Autonomous trading scheduler (~100 lines)
├── requirements.txt
├── Procfile            # web: python app.py | worker: python scheduler.py
│
├── core/
│   ├── config.py       # All env vars, constants, ALPACA_HEADERS — single source of truth
│   ├── database.py     # All SQLite operations: trades table + monitors table
│   ├── market_data.py  # get_stock_price, get_market_overview, fetch_market_snapshot, technicals, news
│   ├── execution.py    # place_order, close_position, get_current_price (Alpaca)
│   ├── analysis.py     # get_trading_session, calculate_vwap, calculate_expected_move, get_0dte_context
│   ├── prediction.py   # Kalshi, Polymarket, Bayesian probability, Kelly Criterion
│   ├── monitor.py      # Background monitor thread with DB-backed persistence + startup recovery
│   └── auth.py         # Token/password auth with rate limiting
│
├── agent/
│   ├── prompts.py      # SYSTEM_PROMPT + SCHEDULER_SYSTEM_PROMPT (share TRADE_CRITERIA block)
│   ├── tools.py        # TOOLS list (27 tool definitions passed to Claude)
│   ├── tool_handlers.py # Tool implementations + run_tool() router
│   └── loop.py         # run_agent() and run_agent_streaming() with parallel tool execution
│
└── tests/
    ├── test_database.py  # DB functions with in-memory SQLite (no external deps)
    ├── test_analysis.py  # Session logic + Bayesian + Kelly (pure math, mockable)
    └── test_monitor.py   # Monitor startup recovery + exit condition logic
```

## Architecture Overview

FinSight is a Flask + Claude AI trading assistant with two operating modes that share the same `core/` library.

### 1. Manual Mode (`app.py`) — ~100 lines

Users chat with Claude via a streaming web UI. The agent loop (`agent/loop.py`) handles multi-turn conversations with parallel tool execution. Claude calls ~27 tools to fetch data and execute trades, all implemented in `agent/tool_handlers.py`.

Key flow: `POST /chat` → `run_agent_streaming()` → tool calls in parallel via `ThreadPoolExecutor` → streams SSE response.

### 2. Autonomous Mode (`scheduler.py`) — ~100 lines

A separate process that runs 3× per trading weekday (9:31 AM, 12:00 PM, 3:30 PM ET). Uses `SCHEDULER_SYSTEM_PROMPT` from `agent/prompts.py` which shares the same `TRADE_CRITERIA` block as the interactive prompt — update it once, both modes stay in sync.

Auto-execution requires **all three**: confidence ≥ `TRADE_CONFIDENCE_THRESHOLD` (default 7), technical momentum, and a news catalyst not yet priced in.

### Cross-process safety

- **Shared DB**: Both processes use the same SQLite DB in WAL mode. `db_has_open_position()` prevents duplicate trades across processes.
- **Persistent monitors**: `db_set_monitor()` / `db_get_active_monitors()` persist to the `monitors` table. The monitor thread calls `_load_monitors_from_db()` on startup — open positions survive process restarts.
- **Duplicate prevention**: `db_set_monitor()` uses `INSERT OR IGNORE` with a `UNIQUE(symbol)` constraint. Returns `False` if a monitor already exists, preventing one process from overwriting another's monitor.

### Monitor thread

`core/monitor.py` runs a background thread checking open positions every 60 seconds. On startup it rehydrates from the `monitors` table so positions aren't stranded after a crash/restart. Auto-closes on stop-loss, take-profit, or time limit, then calls `db_remove_monitor()` and `db_log_exit()`.

`RUN_MONITOR_IN_WEB=true` (default) starts the monitor in the web process. If the scheduler is always deployed, set it to `false` to avoid duplicate monitor threads.

### Data storage

- **SQLite** (`finsight_trades.db`): `trades` table (full trade journal) + `monitors` table (active position monitors)
- **In-memory**: 60-second TTL cache for API responses (`core/market_data.py`)
- **Paper trading**: Uses Alpaca paper API. Change `ALPACA_TRADING_URL` in `core/config.py` to `https://api.alpaca.markets/v2` for live trading.

### Authentication

`core/auth.py` supports:
- **Named tokens** (`ACCESS_TOKENS` env var, comma-separated) — preferred; revoke per-device
- **Legacy password** (`APP_PASSWORD`) — backward compatible
- **Rate limiting**: 5 failed attempts per IP per 15 minutes
- **Session expiry**: 12 hours (`PERMANENT_SESSION_LIFETIME`)
