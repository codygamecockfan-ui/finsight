"""
Claude tool definitions. The TOOLS list is passed to every client.messages.create() call.
"""

TOOLS = [
    {
        "name": "get_stock_price",
        "description": "Get the REAL-TIME stock price, bid/ask, and latest trade info for a ticker via Alpaca. Also handles crypto tickers (BTC, ETH, SOL, etc).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock or crypto ticker e.g. AAPL, TSLA, BTC, ETH"}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "get_options_chain",
        "description": "Get the options chain for a ticker including available strike prices and expiration dates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":          {"type": "string", "description": "Stock ticker symbol"},
                "expiration_date": {"type": "string", "description": "Options expiration date YYYY-MM-DD. Leave empty for next 8 days."},
                "option_type":     {"type": "string", "enum": ["call", "put"]}
            },
            "required": ["ticker", "option_type"]
        }
    },
    {
        "name": "get_financial_news",
        "description": "Get the latest financial news for a company, sector, or topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":        {"type": "string"},
                "num_articles": {"type": "integer", "default": 3}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_market_overview",
        "description": "Get real-time market overview — SPY, QQQ, DIA, IWM, VIX.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_stock_technicals",
        "description": "Get technical indicators and recent price history for a stock.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days":   {"type": "integer", "default": 30}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "place_paper_trade",
        "description": "Place a PAPER trade via Alpaca. Only call after explicit user confirmation and dollar amount. Supports stocks, options, and crypto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol":           {"type": "string",  "description": "Ticker or crypto symbol (e.g. BTC, AAPL)"},
                "side":             {"type": "string",  "enum": ["buy", "sell"]},
                "dollar_amount":    {"type": "number",  "description": "Dollar amount to invest"},
                "asset_type":       {"type": "string",  "enum": ["stock", "option", "crypto"]},
                "current_price":    {"type": "number",  "description": "Current price for qty calculation"},
                "thesis":           {"type": "string",  "description": "Brief trade thesis — why this trade"},
                "confidence":       {"type": "integer", "description": "Confidence level 1-10"},
                "indicators":       {"type": "string",  "description": "Key indicators that triggered this"},
                "market_condition": {"type": "string",  "description": "Current market context"}
            },
            "required": ["symbol", "side", "dollar_amount", "asset_type", "current_price"]
        }
    },
    {
        "name": "get_paper_positions",
        "description": "Get all current open positions in the paper trading account.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_paper_account",
        "description": "Get paper trading account balance, buying power, and portfolio value.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "close_paper_position",
        "description": "Close an open paper trade position.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"]
        }
    },
    {
        "name": "set_trade_monitor",
        "description": "Set autonomous exit rules for a position. Call immediately after placing a trade.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol":          {"type": "string"},
                "entry_price":     {"type": "number"},
                "asset_type":      {"type": "string", "enum": ["stock", "crypto", "option"]},
                "stop_loss_pct":   {"type": "number", "description": "e.g. 0.10 = 10% loss. Use 0 to skip."},
                "take_profit_pct": {"type": "number", "description": "e.g. 0.20 = 20% gain. Use 0 to skip."},
                "time_limit_min":  {"type": "integer", "description": "Minutes until forced sell. Use 0 to skip."}
            },
            "required": ["symbol", "entry_price", "asset_type", "stop_loss_pct", "take_profit_pct", "time_limit_min"]
        }
    },
    {
        "name": "get_monitor_log",
        "description": "Get the log of autonomous trade actions taken by the background monitor.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "cancel_trade_monitor",
        "description": "Cancel the autonomous monitor for a position.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"]
        }
    },
    {
        "name": "get_performance_summary",
        "description": "Get aggregate performance stats from the trade journal — win rate, total P&L, best/worst trades, breakdown by asset type and exit reason.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_recent_trades",
        "description": "Get the last N trades from the journal with full details including thesis, indicators, P&L, and exit reason.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of recent trades to return (default 10)", "default": 10}
            },
            "required": []
        }
    },
    {
        "name": "get_trading_session",
        "description": "Get the current trading session window and quality rating for 0DTE trades.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_vwap",
        "description": "Calculate intraday VWAP for a stock ticker.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker e.g. SPY, QQQ, IWM"}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "get_expected_move",
        "description": "Calculate the market-implied expected move for today using ATM IV.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":      {"type": "string"},
                "option_type": {"type": "string", "enum": ["call", "put"]}
            },
            "required": ["ticker", "option_type"]
        }
    },
    {
        "name": "get_0dte_context",
        "description": "FASTEST way to get all 0DTE pre-trade context in ONE call. Fetches session quality, VWAP, expected move, and current price all in parallel. Use INSTEAD of calling get_trading_session + get_vwap + get_expected_move separately.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":      {"type": "string"},
                "option_type": {"type": "string", "enum": ["call", "put"], "default": "call"}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "get_kalshi_markets",
        "description": "Search live Kalshi prediction markets by keyword.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_kalshi_market_detail",
        "description": "Get full details on a specific Kalshi market by ticker.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "analyze_prediction_market",
        "description": "Full prediction market analysis: Kalshi data + implied probability + edge + Kelly sizing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":          {"type": "string"},
                "our_probability": {"type": "number", "description": "Our estimated probability of YES (0-1)."}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "calculate_bayesian_probability",
        "description": "Run a Bayesian probability update from a base rate and evidence items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "base_rate": {"type": "number"},
                "evidence_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description":      {"type": "string"},
                            "likelihood_ratio": {"type": "number"}
                        }
                    }
                }
            },
            "required": ["base_rate", "evidence_items"]
        }
    },
    {
        "name": "calculate_kelly_criterion",
        "description": "Calculate optimal bet size using Kelly Criterion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "our_probability":  {"type": "number"},
                "market_yes_price": {"type": "number"},
                "bankroll":         {"type": "number", "default": 1000},
                "max_fraction":     {"type": "number", "default": 0.25}
            },
            "required": ["our_probability", "market_yes_price"]
        }
    },
    {
        "name": "get_prediction_market_categories",
        "description": "Get all available prediction market categories on Kalshi with market counts.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_polymarket_markets",
        "description": "Search live Polymarket prediction markets. Covers sports, politics, crypto, macro, world events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_polymarket_market_detail",
        "description": "Get full detail on a specific Polymarket market by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "market_id": {"type": "string"}
            },
            "required": ["market_id"]
        }
    },
    {
        "name": "search_prediction_markets",
        "description": "Search BOTH Kalshi and Polymarket simultaneously. Best first tool for any prediction market query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 8}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_kalshi_balance",
        "description": "Get Kalshi account balance and buying power.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_kalshi_positions",
        "description": "Get all open Kalshi prediction market positions.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_kalshi_orders",
        "description": "Get all open/resting Kalshi orders.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "place_kalshi_order",
        "description": "Place a REAL Kalshi prediction market order. Max $5 per bet. Always show the full prediction market summary and get approval before calling this unless confidence >= 7 AND cost <= $2.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":          {"type": "string", "description": "Kalshi market ticker e.g. FED-25JUN-T5.25"},
                "side":            {"type": "string", "enum": ["yes", "no"]},
                "count":           {"type": "integer", "description": "Number of contracts"},
                "yes_price_cents": {"type": "integer", "description": "Price in cents (1-99)"},
                "action":          {"type": "string", "enum": ["buy", "sell"], "default": "buy"}
            },
            "required": ["ticker", "side", "count", "yes_price_cents"]
        }
    },
    {
        "name": "cancel_kalshi_order",
        "description": "Cancel an open Kalshi order by order ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"}
            },
            "required": ["order_id"]
        }
    },
    {
        "name": "get_kalshi_events",
        "description": "Browse Kalshi events with nested markets. More reliable than market search for finding sports markets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":    {"type": "string"},
                "category": {"type": "string", "description": "e.g. Basketball, Baseball, Hockey, Sports"},
                "limit":    {"type": "integer", "default": 10}
            },
            "required": []
        }
    },
    {
        "name": "get_kalshi_sports_today",
        "description": "Get ALL live and upcoming sports markets on Kalshi today — NBA, MLB, NHL, all sports. Sorted by volume. Use first when looking for any sports bet.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_live_nba_games",
        "description": "Get all NBA games today with live scores, current quarter, time remaining, and quarter-by-quarter breakdown. Use to see games live RIGHT NOW before any NBA prediction market bet.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_nba_injuries",
        "description": "Get the current NBA injury report — all players listed as Out, Questionable, or Doubtful.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_nba_standings",
        "description": "Get current NBA standings for both conferences — win%, home/away record, last 10 games, streak.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_nba_team_context",
        "description": "Get full betting context for an NBA team — current standing, last 10 games with results, and active injuries. Run on BOTH teams before any NBA prediction market analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team_name": {"type": "string", "description": "Team name e.g. Celtics, Lakers, Warriors, Knicks"}
            },
            "required": ["team_name"]
        }
    }
]
