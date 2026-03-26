import os
from dotenv import load_dotenv

load_dotenv()

# ── Anthropic ──────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# ── Alpaca ─────────────────────────────────────────────────
ALPACA_API_KEY     = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = os.getenv("ALPACA_SECRET_KEY")
ALPACA_DATA_URL    = "https://data.alpaca.markets/v2"
ALPACA_CRYPTO_URL  = "https://data.alpaca.markets/v1beta3/crypto/us"
ALPACA_TRADING_URL = "https://paper-api.alpaca.markets/v2"  # swap to api.alpaca.markets for live

ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "Content-Type":        "application/json",
}

CRYPTO_SYMBOLS = {"BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "MATIC", "LTC", "BCH", "XRP", "ADA"}

# ── Other API keys ──────────────────────────────────────────
POLYGON_API_KEY   = os.getenv("POLYGON_API_KEY")
NEWS_API_KEY      = os.getenv("NEWS_API_KEY")
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY", "")
KALSHI_BASE_URL   = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_BASE_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_URL = "https://clob.polymarket.com"

# ── Trading parameters ──────────────────────────────────────
TRADE_CONFIDENCE_THRESHOLD = int(os.getenv("AUTO_EXECUTE_THRESHOLD", "7"))
AUTO_EXECUTE_MAX_DOLLAR    = float(os.getenv("AUTO_EXECUTE_MAX_DOLLAR", "200"))
MAX_TRADE_AMOUNT           = float(os.getenv("MAX_TRADE_AMOUNT", "500"))
MAX_OPTIONS_PREMIUM        = float(os.getenv("MAX_OPTIONS_PREMIUM", "0.80"))

# ── Database ────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "finsight_trades.db"))

# ── Web app ─────────────────────────────────────────────────
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", os.urandom(24))
APP_PASSWORD     = os.getenv("APP_PASSWORD", "")
# Named tokens: comma-separated list, e.g. "my-laptop-token,phone-token"
ACCESS_TOKENS    = [t.strip() for t in os.getenv("ACCESS_TOKENS", "").split(",") if t.strip()]
