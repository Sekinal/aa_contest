"""Data models and enums for the AA scraper"""

from enum import Enum


class CircuitState(Enum):
    """Circuit breaker states"""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if recovered


class ErrorType(Enum):
    """Error categories for different handling strategies"""

    TRANSIENT = "transient"  # Retry immediately
    RATE_LIMIT = "rate_limit"  # Backoff and retry
    AUTH_FAILURE = "auth_failure"  # Need fresh cookies
    PERMANENT = "permanent"  # Don't retry