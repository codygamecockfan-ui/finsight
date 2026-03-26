"""
Authentication helpers.
Supports named access tokens (ACCESS_TOKENS env var, comma-separated)
with backward compatibility for the legacy APP_PASSWORD env var.
Includes simple in-memory rate limiting (5 attempts per 15 minutes per IP).
"""
import hmac
import hashlib
from datetime import datetime, timedelta
from core.config import APP_PASSWORD, ACCESS_TOKENS

_failed_attempts: dict[str, list] = {}
_MAX_ATTEMPTS  = 5
_WINDOW_MINUTES = 15


def _constant_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def check_token(provided: str) -> bool:
    """
    Validates a password or token against configured credentials.
    Tries named tokens first, then falls back to APP_PASSWORD.
    """
    if ACCESS_TOKENS:
        return any(_constant_compare(provided, token) for token in ACCESS_TOKENS)
    if APP_PASSWORD:
        # Legacy: SHA-256 comparison kept for backward compat
        hashed = hashlib.sha256(provided.encode()).hexdigest()
        expected = hashlib.sha256(APP_PASSWORD.encode()).hexdigest()
        return _constant_compare(hashed, expected)
    return False


def check_rate_limit(ip: str) -> bool:
    """Returns True if this IP is within the rate limit (not blocked)."""
    now    = datetime.now()
    cutoff = now - timedelta(minutes=_WINDOW_MINUTES)
    attempts = _failed_attempts.get(ip, [])
    # Prune old attempts
    attempts = [t for t in attempts if t > cutoff]
    _failed_attempts[ip] = attempts
    return len(attempts) < _MAX_ATTEMPTS


def record_failed_attempt(ip: str):
    now = datetime.now()
    _failed_attempts.setdefault(ip, []).append(now)
