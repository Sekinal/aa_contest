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


class IPBlockedError(AAScraperError):
    """Raised when IP is blocked by the server (Permission Denied)
    
    This indicates the IP address has been temporarily blocked by the server,
    not by Akamai. This is a server-level block that typically lasts ~20 minutes,
    but waiting ~40 minutes is recommended before retrying.
    
    Retrying too soon (~20 minutes) may result in immediate re-blocking.
    """
    
    def __init__(self, message: str = "IP blocked by server (Permission Denied)"):
        self.recommended_wait_minutes = 40
        self.minimum_wait_minutes = 20
        super().__init__(message)