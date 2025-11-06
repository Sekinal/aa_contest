"""Configuration constants for the AA scraper"""

from pathlib import Path

# API Configuration
API_ENDPOINT = "https://www.aa.com/booking/api/search/itinerary"
BASE_URL = "https://www.aa.com"
DEFAULT_COOKIE_FILE = Path("./cookies/aa_cookies.json")

# Cookie age thresholds (in seconds)
COOKIE_MAX_AGE = 1800  # 30 minutes - refresh after this
COOKIE_WARNING_AGE = 1200  # 20 minutes - warn but still use

# Retry configuration
MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 60.0
BACKOFF_MULTIPLIER = 2.0
JITTER_RANGE = (0.8, 1.2)

# Circuit breaker configuration
CIRCUIT_BREAKER_THRESHOLD = 3  # Failures before opening circuit
CIRCUIT_BREAKER_TIMEOUT = 300  # 5 minutes before trying again

# Cabin class mapping
CABIN_CLASS_MAP = {
    "COACH": "economy",
    "BUSINESS": "business",
    "FIRST": "first",
    "PREMIUM_ECONOMY": "premium_economy",
}

# Rate limiting defaults
DEFAULT_RATE_LIMIT = 10.0  # Requests per second
DEFAULT_BURST = 20  # Burst capacity

# Timeouts
DEFAULT_REQUEST_TIMEOUT = 10.0  # Seconds
DEFAULT_COOKIE_WAIT_TIME = 15  # Seconds

# Test flight parameters for cookie validation
DEFAULT_TEST_ORIGIN = "SRQ"
DEFAULT_TEST_DESTINATION = "BFL"
DEFAULT_TEST_DAYS_AHEAD = 7