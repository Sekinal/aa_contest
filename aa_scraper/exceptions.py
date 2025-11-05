"""Custom exception classes for the AA scraper"""


class AAScraperError(Exception):
    """Base exception for scraper errors"""

    pass


class CookieExpiredError(AAScraperError):
    """Raised when cookies are expired or invalid"""

    pass


class RateLimitError(AAScraperError):
    """Raised when rate limited"""

    pass


class CircuitOpenError(AAScraperError):
    """Raised when circuit breaker is open"""

    pass