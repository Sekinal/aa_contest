"""
American Airlines Flight Scraper
Production-ready async scraper with advanced bot evasion
"""

__version__ = "0.2.0"

from .api_client import AAFlightClient
from .circuit_breaker import CircuitBreaker
from .cookie_manager import CookieManager
from .exceptions import (
    AAScraperError,
    CircuitOpenError,
    CookieExpiredError,
    RateLimitError,
)
from .models import CircuitState, ErrorType
from .parser import FlightDataParser
from .rate_limiter import AdaptiveRateLimiter
from .cookie_pool import CookiePool

__all__ = [
    "__version__",
    "AAFlightClient",
    "CircuitBreaker",
    "CookieManager",
    "CookiePool",
    "AAScraperError",
    "CircuitOpenError",
    "CookieExpiredError",
    "RateLimitError",
    "CircuitState",
    "ErrorType",
    "FlightDataParser",
    "AdaptiveRateLimiter",
]