#!/usr/bin/env python3
"""
American Airlines Flight Scraper - Operation Point Break
Production-ready async scraper with advanced bot evasion and automatic recovery
Features: Auto cookie refresh, exponential backoff, circuit breaker, health checks
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from enum import Enum
import random

import httpx
from loguru import logger

# ============================================================================
# Configuration
# ============================================================================

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
    "PREMIUM_ECONOMY": "premium_economy"
}

# ============================================================================
# Enhanced Logging Setup with Loguru
# ============================================================================

def setup_logging(verbose: bool = False, log_file: Optional[Path] = None):
    """Configure loguru for production logging"""
    # Remove default handler
    logger.remove()
    
    # Console handler with colors
    log_level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level=log_level,
        colorize=True,
    )
    
    # File handler with rotation
    if log_file:
        logger.add(
            log_file,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
            level="DEBUG",
            rotation="100 MB",
            retention="30 days",
            compression="zip",
            enqueue=True,  # Thread-safe
        )
        logger.info(f"Logging to file: {log_file}")

# ============================================================================
# Enums for Better Type Safety
# ============================================================================

class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if recovered

class ErrorType(Enum):
    """Error categories for different handling strategies"""
    TRANSIENT = "transient"  # Retry immediately
    RATE_LIMIT = "rate_limit"  # Backoff and retry
    AUTH_FAILURE = "auth_failure"  # Need fresh cookies
    PERMANENT = "permanent"  # Don't retry

# ============================================================================
# Exception Classes
# ============================================================================

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

# ============================================================================
# Circuit Breaker
# ============================================================================

class CircuitBreaker:
    """
    Circuit breaker pattern to prevent cascading failures.
    Opens after threshold failures, closes after timeout.
    """
    
    def __init__(
        self,
        failure_threshold: int = CIRCUIT_BREAKER_THRESHOLD,
        timeout: float = CIRCUIT_BREAKER_TIMEOUT,
        name: str = "default"
    ):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.name = name
        self.failures = 0
        self.state = CircuitState.CLOSED
        self.last_failure_time = None
        self.lock = asyncio.Lock()
        
        logger.debug(f"Circuit breaker '{name}' initialized: threshold={failure_threshold}, timeout={timeout}s")
    
    async def call(self, func, *args, **kwargs):
        """Execute function with circuit breaker protection"""
        async with self.lock:
            # Check if circuit should transition from OPEN to HALF_OPEN
            if self.state == CircuitState.OPEN:
                if self.last_failure_time and (datetime.now().timestamp() - self.last_failure_time) >= self.timeout:
                    logger.info(f"Circuit '{self.name}' transitioning to HALF_OPEN (timeout expired)")
                    self.state = CircuitState.HALF_OPEN
                    self.failures = 0
                else:
                    remaining = self.timeout - (datetime.now().timestamp() - self.last_failure_time)
                    raise CircuitOpenError(f"Circuit '{self.name}' is OPEN, retry in {remaining:.0f}s")
        
        try:
            result = await func(*args, **kwargs)
            
            # Success - close circuit if it was half-open
            async with self.lock:
                if self.state == CircuitState.HALF_OPEN:
                    logger.success(f"Circuit '{self.name}' recovered, closing")
                    self.state = CircuitState.CLOSED
                    self.failures = 0
            
            return result
            
        except Exception as e:
            async with self.lock:
                self.failures += 1
                self.last_failure_time = datetime.now().timestamp()
                
                if self.failures >= self.failure_threshold:
                    logger.error(f"Circuit '{self.name}' OPENING after {self.failures} failures")
                    self.state = CircuitState.OPEN
                else:
                    logger.warning(f"Circuit '{self.name}' failure {self.failures}/{self.failure_threshold}")
            
            raise

# ============================================================================
# Cookie Management with Auto-Refresh
# ============================================================================

class CookieManager:
    """
    Manages cookies with automatic refresh and validation.
    Tracks cookie age and automatically extracts when needed.
    """
    
    def __init__(
        self,
        cookie_file: Path,
        test_origin: str = "SRQ",
        test_destination: str = "BFL",
        test_days_ahead: int = 7
    ):
        self.cookie_file = cookie_file
        self.test_origin = test_origin
        self.test_destination = test_destination
        self.test_days_ahead = test_days_ahead
        
        self.cookies: Dict[str, str] = {}
        self.headers: Dict[str, str] = {}
        self.referer: str = ""
        self.extract_time: Optional[datetime] = None
        self.lock = asyncio.Lock()
        
        logger.info(f"Cookie manager initialized: {cookie_file}")
    
    def _get_cookie_age(self) -> Optional[float]:
        """Get age of cookies in seconds"""
        if not self.extract_time:
            # Try to get from file modification time
            if self.cookie_file.exists():
                mtime = self.cookie_file.stat().st_mtime
                return datetime.now().timestamp() - mtime
            return None
        
        return (datetime.now() - self.extract_time).total_seconds()
    
    def _is_cookie_valid(self) -> bool:
        """Check if cookies are still valid (not expired)"""
        age = self._get_cookie_age()
        if age is None:
            return False
        
        if age > COOKIE_MAX_AGE:
            logger.warning(f"Cookies expired: {age:.0f}s old (max: {COOKIE_MAX_AGE}s)")
            return False
        
        if age > COOKIE_WARNING_AGE:
            logger.warning(f"Cookies aging: {age:.0f}s old (warn: {COOKIE_WARNING_AGE}s)")
        
        # Check critical cookies exist
        critical = ['XSRF-TOKEN', 'spa_session_id']
        missing = [c for c in critical if c not in self.cookies]
        if missing:
            logger.warning(f"Missing critical cookies: {missing}")
            return False
        
        return True
    
    async def get_cookies(
        self,
        force_refresh: bool = False,
        headless: bool = True,
        wait_time: int = 15
    ) -> Tuple[Dict[str, str], Dict[str, str], str]:
        """
        Get cookies with automatic refresh if needed.
        Thread-safe with lock.
        """
        async with self.lock:
            # Load from file if not in memory
            if not self.cookies and self.cookie_file.exists():
                logger.info("Loading cookies from file...")
                self._load_from_file()
            
            # Check if refresh needed
            needs_refresh = (
                force_refresh or
                not self.cookies or
                not self._is_cookie_valid()
            )
            
            if needs_refresh:
                logger.info("Extracting fresh cookies...")
                await self._extract_fresh_cookies(headless, wait_time)
            else:
                age = self._get_cookie_age()
                logger.info(f"Using cached cookies (age: {age:.0f}s)")
            
            return self.cookies, self.headers, self.referer
    
    def _load_from_file(self):
        """Load cookies and headers from files"""
        try:
            # Load cookies
            if self.cookie_file.exists():
                data = json.loads(self.cookie_file.read_text())
                self.cookies = data
                logger.info(f"Loaded {len(self.cookies)} cookies from {self.cookie_file}")
            
            # Load headers
            headers_file = self.cookie_file.parent / f"{self.cookie_file.stem}_headers.json"
            if headers_file.exists():
                self.headers = json.loads(headers_file.read_text())
                logger.info(f"Loaded {len(self.headers)} headers")
            
            # Load referer
            referer_file = self.cookie_file.parent / f"{self.cookie_file.stem}_referer.txt"
            if referer_file.exists():
                self.referer = referer_file.read_text().strip()
                logger.info(f"Loaded referer")
                
        except Exception as e:
            logger.error(f"Failed to load cookies from file: {e}")
    
    async def _extract_fresh_cookies(self, headless: bool, wait_time: int):
        """Extract fresh cookies using Camoufox - FIXED VERSION"""
        from camoufox.async_api import AsyncCamoufox
        
        logger.info(f"🦊 Extracting cookies: {self.test_origin} → {self.test_destination}")
        
        departure_date = (datetime.now() + timedelta(days=self.test_days_ahead)).strftime("%m/%d/%Y")
        
        captured_headers = {}
        captured_referer = ""
        captured_cookies = {}
        
        try:
            async with AsyncCamoufox(headless=headless) as browser:
                page = await browser.new_page()
                
                # Request interception for headers
                api_request_captured = False
                
                async def handle_request(route):
                    request = route.request
                    nonlocal api_request_captured, captured_headers
                    
                    if "/booking/api/search/itinerary" in request.url:
                        if not api_request_captured:
                            logger.debug("🎯 Intercepted API call, capturing headers")
                            raw_headers = dict(request.headers)
                            captured_headers = self._clean_headers(raw_headers)
                            api_request_captured = True
                            logger.debug(f"   Captured {len(captured_headers)} headers")
                    
                    await route.continue_()
                
                await page.route("**/*", handle_request)
                
                # Step 1: Navigate to homepage
                logger.info("Step 1/4: Loading homepage...")
                await page.goto(f"{BASE_URL}/?locale=en_US", wait_until="networkidle")
                await page.wait_for_timeout(3000)
                
                # Handle cookie consent - THIS WAS IN THE ORIGINAL
                try:
                    accept_btn = await page.query_selector('#onetrust-accept-btn-handler, #accept-recommended-btn-handler')
                    if accept_btn:
                        await accept_btn.click()
                        logger.debug("✓ Accepted cookie consent")
                        await page.wait_for_timeout(1000)
                except:
                    pass
                
                # Step 2: Fill search form - RESTORE ORIGINAL APPROACH
                logger.info("Step 2/4: Filling search form...")
                
                try:
                    await page.wait_for_selector('input[name="originAirport"]', timeout=10000, state="visible")
                    await page.wait_for_timeout(2000)
                    
                    # Verify one-way is selected
                    one_way_checked = await page.is_checked('input[id="flightSearchForm.tripType.oneWay"]')
                    if not one_way_checked:
                        await page.click('label[for="flightSearchForm.tripType.oneWay"]')
                        await page.wait_for_timeout(500)
                    logger.debug("✓ One-way trip selected")
                    
                    # Fill origin
                    origin_input = await page.query_selector('input[name="originAirport"]')
                    await origin_input.click()
                    await page.wait_for_timeout(500)
                    
                    await page.evaluate(f'''
                        const originInput = document.querySelector('input[name="originAirport"]');
                        originInput.value = "{self.test_origin}";
                        originInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        originInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    ''')
                    await page.wait_for_timeout(1000)
                    
                    # Fill destination
                    dest_input = await page.query_selector('input[name="destinationAirport"]')
                    await dest_input.click()
                    await page.wait_for_timeout(500)
                    
                    await page.evaluate(f'''
                        const destInput = document.querySelector('input[name="destinationAirport"]');
                        destInput.value = "{self.test_destination}";
                        destInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        destInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    ''')
                    await page.wait_for_timeout(1000)
                    
                    # Fill date
                    date_input = await page.query_selector('input[name="departDate"]')
                    await date_input.click()
                    await page.wait_for_timeout(500)
                    
                    await page.evaluate(f'''
                        const dateInput = document.querySelector('input[name="departDate"]');
                        dateInput.value = "{departure_date}";
                        dateInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        dateInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    ''')
                    await page.wait_for_timeout(1000)
                    
                    logger.debug(f"✓ Form filled: {self.test_origin} → {self.test_destination} on {departure_date}")
                    
                except Exception as e:
                    logger.error(f"❌ Failed to fill form: {e}")
                    await page.screenshot(path="error_form_fill.png")
                    raise
                
                # Step 3: Submit search
                logger.info("Step 3/4: Submitting search...")
                try:
                    submit_btn = await page.query_selector('input[type="submit"][id="flightSearchForm.button.reSubmit"]')
                    if not submit_btn:
                        raise Exception("Submit button not found")
                    
                    try:
                        async with page.expect_navigation(wait_until="networkidle", timeout=60000):
                            await submit_btn.click()
                            logger.debug("✓ Search button clicked, waiting for navigation...")
                    except Exception as nav_error:
                        logger.warning(f"⚠️ Navigation wait failed: {nav_error}")
                    
                    await page.wait_for_timeout(5000)
                    
                    current_url = page.url
                    captured_referer = current_url
                    logger.debug(f"📍 Current URL: {current_url}")
                    
                    if any(pattern in current_url for pattern in ["/choose-flights", "/flights", "/booking/find-flights"]):
                        logger.debug("✅ Reached flight results page!")
                    elif "homePage" in current_url:
                        logger.error("❌ Form submission failed - still on homepage")
                        raise Exception("Form submission failed")
                    else:
                        logger.warning(f"⚠️ Unexpected URL: {current_url}")
                    
                except Exception as e:
                    logger.error(f"❌ Failed to submit search: {e}")
                    await page.screenshot(path="error_submit.png")
                    raise
                
                # Step 4: Wait for results and API calls
                logger.info(f"Step 4/4: Waiting {wait_time}s for API calls...")
                await page.wait_for_timeout(wait_time * 1000)
                
                # Check if API call was intercepted
                if not api_request_captured:
                    logger.warning("⚠️ Did not intercept API call - trying to trigger it...")
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(3000)
                
                # Extract cookies
                raw_cookies = await page.context.cookies()
                for cookie in raw_cookies:
                    captured_cookies[cookie['name']] = cookie['value']
                
                logger.debug(f"✓ Extracted {len(captured_cookies)} cookies")
                
                # Validate critical cookies
                critical_cookies = ['XSRF-TOKEN', 'spa_session_id', 'JSESSIONID']
                found_critical = [c for c in critical_cookies if c in captured_cookies]
                
                if found_critical:
                    logger.debug(f"  ✓ Found critical cookies: {', '.join(found_critical)}")
                
                bot_cookies = ['_abck', 'bm_sz', 'ak_bmsc', 'bm_sv']
                found_bot = [c for c in bot_cookies if c in captured_cookies]
                if found_bot:
                    logger.debug(f"  ✓ Found bot-defense cookies: {', '.join(found_bot)}")
            
            # Success - update state
            self.cookies = captured_cookies
            self.headers = captured_headers
            self.referer = captured_referer
            self.extract_time = datetime.now()
            
            self._save_to_file()
            
            logger.success(f"✓ Cookie extraction successful: {len(captured_cookies)} cookies, {len(captured_headers)} headers")
            
        except Exception as e:
            logger.error(f"Cookie extraction failed: {e}")
            raise CookieExpiredError(f"Failed to extract cookies: {e}")
        
    def _clean_headers(self, raw_headers: Dict[str, str]) -> Dict[str, str]:
        """Clean headers for httpx use"""
        SKIP = {'host', 'content-length', 'connection', 'cookie', 'accept-encoding'}
        return {k: v for k, v in raw_headers.items() 
                if not k.lower().startswith(':') and k.lower() not in SKIP}
    
    def _save_to_file(self):
        """Save cookies, headers, and referer to files"""
        try:
            self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Save cookies
            self.cookie_file.write_text(json.dumps(self.cookies, indent=2))
            
            # Save headers
            headers_file = self.cookie_file.parent / f"{self.cookie_file.stem}_headers.json"
            headers_file.write_text(json.dumps(self.headers, indent=2))
            
            # Save referer
            referer_file = self.cookie_file.parent / f"{self.cookie_file.stem}_referer.txt"
            referer_file.write_text(self.referer)
            
            logger.info(f"💾 Saved cookies to: {self.cookie_file}")
            
        except Exception as e:
            logger.error(f"Failed to save cookies: {e}")

# ============================================================================
# Enhanced Rate Limiter with Exponential Backoff
# ============================================================================

class AdaptiveRateLimiter:
    """
    Advanced rate limiter with exponential backoff and jitter.
    Adapts to rate limit errors automatically.
    """
    
    def __init__(self, rate: float = 1.0, burst: int = 3):
        self.base_rate = rate
        self.current_rate = rate
        self.burst = burst
        self.tokens = burst
        self.last_update = asyncio.get_event_loop().time()
        self.lock = asyncio.Lock()
        self.backoff_until = None
        
        logger.debug(f"Rate limiter initialized: {rate} req/s, burst={burst}")
    
    async def acquire(self):
        """Acquire a token with adaptive backoff"""
        async with self.lock:
            # Check if in backoff period
            if self.backoff_until:
                now = asyncio.get_event_loop().time()
                if now < self.backoff_until:
                    wait_time = self.backoff_until - now
                    logger.warning(f"Rate limiter in backoff: waiting {wait_time:.1f}s")
                    await asyncio.sleep(wait_time)
                self.backoff_until = None
            
            # Token bucket algorithm
            now = asyncio.get_event_loop().time()
            elapsed = now - self.last_update
            self.tokens = min(self.burst, self.tokens + elapsed * self.current_rate)
            self.last_update = now
            
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            
            wait_time = (1.0 - self.tokens) / self.current_rate
            await asyncio.sleep(wait_time)
            self.tokens = 0.0
    
    async def backoff(self, duration: float):
        """Enter backoff period (e.g., when rate limited)"""
        async with self.lock:
            self.backoff_until = asyncio.get_event_loop().time() + duration
            self.current_rate = max(0.1, self.current_rate * 0.5)  # Reduce rate
            logger.warning(f"Rate limited! Backing off for {duration:.1f}s, new rate: {self.current_rate:.2f} req/s")
    
    async def recover(self):
        """Recover rate limit after success"""
        async with self.lock:
            old_rate = self.current_rate
            self.current_rate = min(self.base_rate, self.current_rate * 1.2)
            if self.current_rate != old_rate:
                logger.info(f"Rate limiter recovering: {old_rate:.2f} → {self.current_rate:.2f} req/s")

# ============================================================================
# Retry Logic with Exponential Backoff
# ============================================================================

async def retry_with_backoff(
    func,
    *args,
    max_retries: int = MAX_RETRIES,
    initial_backoff: float = INITIAL_BACKOFF,
    max_backoff: float = MAX_BACKOFF,
    backoff_multiplier: float = BACKOFF_MULTIPLIER,
    on_retry: Optional[callable] = None,
    **kwargs
):
    """
    Execute function with exponential backoff retry logic.
    
    Args:
        func: Async function to execute
        max_retries: Maximum number of retry attempts
        initial_backoff: Initial backoff duration in seconds
        max_backoff: Maximum backoff duration
        backoff_multiplier: Multiplier for exponential backoff
        on_retry: Optional callback called on each retry: on_retry(attempt, error)
    """
    last_exception = None
    backoff = initial_backoff
    
    for attempt in range(max_retries + 1):
        try:
            result = await func(*args, **kwargs)
            
            # Success - log recovery if this was a retry
            if attempt > 0:
                logger.success(f"✓ Recovered after {attempt} retries")
            
            return result
            
        except Exception as e:
            last_exception = e
            
            # Check if we should retry
            if attempt >= max_retries:
                logger.error(f"❌ Failed after {max_retries} retries: {e}")
                break
            
            # Calculate backoff with jitter
            jitter = random.uniform(*JITTER_RANGE)
            sleep_time = min(backoff * jitter, max_backoff)
            
            error_type = _classify_error(e)
            logger.warning(f"⚠️ Attempt {attempt + 1}/{max_retries + 1} failed ({error_type.value}): {e}")
            logger.info(f"   Retrying in {sleep_time:.1f}s...")
            
            # Call retry callback if provided
            if on_retry:
                await on_retry(attempt, e)
            
            await asyncio.sleep(sleep_time)
            backoff *= backoff_multiplier
    
    raise last_exception

def _classify_error(error: Exception) -> ErrorType:
    """Classify error for appropriate handling"""
    if isinstance(error, CookieExpiredError):
        return ErrorType.AUTH_FAILURE
    elif isinstance(error, RateLimitError):
        return ErrorType.RATE_LIMIT
    elif isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code == 403:
            return ErrorType.AUTH_FAILURE
        elif error.response.status_code == 429:
            return ErrorType.RATE_LIMIT
        elif error.response.status_code >= 500:
            return ErrorType.TRANSIENT
        else:
            return ErrorType.PERMANENT
    elif isinstance(error, (httpx.ConnectError, httpx.TimeoutException)):
        return ErrorType.TRANSIENT
    else:
        return ErrorType.PERMANENT

# ============================================================================
# Enhanced API Client with Auto-Recovery
# ============================================================================

class AAFlightClient:
    """
    Enhanced AA flight search client with:
    - Automatic cookie refresh on 403 errors
    - Circuit breaker pattern
    - Exponential backoff retry
    - Request health checks
    """
    
    def __init__(
        self,
        cookie_manager: CookieManager,
        rate_limiter: AdaptiveRateLimiter,
        timeout: float = 30.0
    ):
        self.cookie_manager = cookie_manager
        self.rate_limiter = rate_limiter
        self.timeout = timeout
        self.circuit_breaker = CircuitBreaker(name="aa_api")
        self.session_start = datetime.now()
        
        logger.info("Flight client initialized with auto-recovery")
    
    def _build_headers(self, cookies: Dict[str, str], captured_headers: Dict[str, str], referer: str) -> Dict[str, str]:
        """Build request headers with proper ordering"""
        HEADER_ORDER = [
            'user-agent', 'accept', 'accept-language', 'content-type',
            'referer', 'x-xsrf-token', 'x-cid', 'origin',
            'sec-fetch-dest', 'sec-fetch-mode', 'sec-fetch-site', 'priority', 'te',
        ]
        
        if captured_headers:
            captured_lower = {k.lower(): (k, v) for k, v in captured_headers.items()}
            headers = {}
            
            for header_name in HEADER_ORDER:
                if header_name in captured_lower:
                    original_key, value = captured_lower[header_name]
                    headers[original_key] = value
            
            # Add remaining headers
            for key, value in captured_headers.items():
                if key.lower() not in [h.lower() for h in headers.keys()]:
                    headers[key] = value
            
            # Override referer
            if referer:
                for key in headers.keys():
                    if key.lower() == 'referer':
                        headers[key] = referer
                        break
                else:
                    headers['Referer'] = referer
        else:
            # Fallback headers
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US",
                "Content-Type": "application/json",
                "Origin": BASE_URL,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
            if referer:
                headers["Referer"] = referer
        
        # Ensure critical headers
        headers_lower = {k.lower(): k for k in headers.keys()}
        if 'x-xsrf-token' not in headers_lower and "XSRF-TOKEN" in cookies:
            headers["X-XSRF-TOKEN"] = cookies["XSRF-TOKEN"]
        if 'x-cid' not in headers_lower and "spa_session_id" in cookies:
            headers["X-CID"] = cookies["spa_session_id"]
        if 'user-agent' not in headers_lower:
            headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0"
        
        return headers
    
    def _build_request_payload(
        self,
        origin: str,
        destination: str,
        date: str,
        passengers: int,
        search_type: str
    ) -> Dict[str, Any]:
        """Build API request payload"""
        payload = {
            "metadata": {
                "selectedProducts": [],
                "tripType": "OneWay",
                "udo": {}
            },
            "passengers": [{"type": "adult", "count": passengers}],
            "requestHeader": {"clientId": "AAcom"},
            "slices": [{
                "allCarriers": True,
                "cabin": "",
                "connectionCity": None,
                "departureDate": date,
                "destination": destination,
                "destinationNearbyAirports": False,
                "maxStops": None,
                "origin": origin,
                "originNearbyAirports": False
            }],
            "tripOptions": {
                "corporateBooking": False,
                "fareType": "Lowest",
                "locale": "en_US",
                "pointOfSale": "",
                "searchType": search_type
            },
            "loyaltyInfo": None,
            "version": "cfr" if search_type == "Revenue" else "",
            "queryParams": {
                "sliceIndex": 0,
                "sessionId": "",
                "solutionSet": "",
                "solutionId": "",
                "sort": "CARRIER"
            }
        }
        
        if search_type == "Revenue":
            payload["metadata"]["udo"]["search_method"] = "Lowest"
        
        return payload
    
    async def _make_request(
        self,
        origin: str,
        destination: str,
        date: str,
        passengers: int,
        search_type: str,
        http_version: str = "HTTP/2"
    ) -> Dict[str, Any]:
        """Make a single API request"""
        # Get fresh cookies
        cookies, captured_headers, referer = await self.cookie_manager.get_cookies()
        headers = self._build_headers(cookies, captured_headers, referer)
        payload = self._build_request_payload(origin, destination, date, passengers, search_type)
        
        # Acquire rate limit token
        await self.rate_limiter.acquire()
        
        # Make request
        http2_enabled = (http_version == "HTTP/2")
        limits = httpx.Limits(
            max_keepalive_connections=5,
            max_connections=10,
            keepalive_expiry=30.0
        )
        
        async with httpx.AsyncClient(
            cookies=cookies,
            headers=headers,
            timeout=self.timeout,
            limits=limits,
            http2=http2_enabled,
            follow_redirects=True
        ) as client:
            logger.info(f"🔍 {search_type}: {origin} → {destination} on {date} (via {http_version})")
            
            response = await client.post(API_ENDPOINT, json=payload)
            
            logger.debug(f"Response: {response.status_code}")
            
            # Handle specific status codes
            if response.status_code == 403:
                logger.warning("Got 403 - bot detection triggered")
                raise CookieExpiredError("403 Forbidden - cookies may be invalid")
            
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 60))
                logger.warning(f"Rate limited - retry after {retry_after}s")
                await self.rate_limiter.backoff(retry_after)
                raise RateLimitError(f"Rate limited, retry after {retry_after}s")
            
            response.raise_for_status()
            
            # Success - recover rate limiter
            await self.rate_limiter.recover()
            
            return response.json()
    
    async def search_flights(
        self,
        origin: str,
        destination: str,
        date: str,
        passengers: int,
        search_type: str = "Award"
    ) -> Optional[Dict[str, Any]]:
        """
        Search for flights with automatic recovery.
        Tries HTTP/2, falls back to HTTP/1.1.
        Auto-refreshes cookies on 403 errors.
        """
        
        async def attempt_search():
            """Single search attempt across HTTP versions"""
            for http_version in ["HTTP/2", "HTTP/1.1"]:
                try:
                    return await self._make_request(
                        origin, destination, date, passengers, search_type, http_version
                    )
                except (httpx.StreamError, httpx.HTTPStatusError) as e:
                    if http_version == "HTTP/2":
                        logger.warning(f"HTTP/2 failed, trying HTTP/1.1: {e}")
                        continue
                    raise
            
            return None
        
        async def on_retry_callback(attempt: int, error: Exception):
            """Handle retry attempts"""
            error_type = _classify_error(error)
            
            if error_type == ErrorType.AUTH_FAILURE:
                logger.warning("Auth failure detected - refreshing cookies...")
                try:
                    await self.cookie_manager.get_cookies(force_refresh=True, headless=True)
                    logger.success("✓ Cookies refreshed")
                except Exception as e:
                    logger.error(f"Failed to refresh cookies: {e}")
            
            elif error_type == ErrorType.RATE_LIMIT:
                logger.warning("Rate limit detected - increasing backoff...")
                await self.rate_limiter.backoff(30)
        
        try:
            # Use circuit breaker for protection
            result = await self.circuit_breaker.call(
                retry_with_backoff,
                attempt_search,
                max_retries=MAX_RETRIES,
                on_retry=on_retry_callback
            )
            
            if result:
                logger.success(f"✅ {search_type} search successful")
            
            return result
            
        except CircuitOpenError as e:
            logger.error(f"Circuit breaker open: {e}")
            return None
        except Exception as e:
            logger.error(f"Search failed after all retries: {e}")
            return None

# ============================================================================
# Data Parser (unchanged, keeping your implementation)
# ============================================================================

class FlightDataParser:
    """Parse AA API response into structured data"""
    
    @staticmethod
    def parse_flight_options(
        api_response: Dict[str, Any],
        cabin_filter: str = "COACH",
        search_type: str = "Award"
    ) -> List[Dict[str, Any]]:
        """Parse flight options from API response"""
        flights = []
        slices = api_response.get("slices", [])
        
        for slice_data in slices:
            duration_min = slice_data.get("durationInMinutes", 0)
            duration_str = format_duration(duration_min)
            is_nonstop = slice_data.get("stops", 0) == 0
            
            segments_data = slice_data.get("segments", [])
            parsed_segments = []
            
            for segment in segments_data:
                flight_info = segment.get("flight", {})
                carrier_code = flight_info.get("carrierCode", "")
                flight_num = flight_info.get("flightNumber", "")
                flight_number = f"{carrier_code}{flight_num}"
                
                dep_time = format_time(segment.get("departureDateTime", ""))
                arr_time = format_time(segment.get("arrivalDateTime", ""))
                
                parsed_segments.append({
                    "flight_number": flight_number,
                    "departure_time": dep_time,
                    "arrival_time": arr_time
                })
            
            if not parsed_segments:
                continue
            
            pricing_detail = slice_data.get("pricingDetail", [])
            
            for pricing_option in pricing_detail:
                if not pricing_option.get("productAvailable", False):
                    continue
                
                product_type = pricing_option.get("productType", "")
                
                if cabin_filter == "COACH" and product_type != "COACH":
                    continue
                elif cabin_filter != "COACH" and not product_type.startswith(cabin_filter):
                    continue
                
                slice_pricing = pricing_option.get("slicePricing", {})
                if not slice_pricing:
                    continue
                
                points_str = slice_pricing.get("perPassengerAwardPoints", "0")
                if isinstance(points_str, str):
                    points_or_fare = float(points_str.replace(",", ""))
                else:
                    points_or_fare = float(points_str)
                
                if search_type == "Award":
                    points = int(points_or_fare)
                else:
                    points = 0
                
                taxes_fees = slice_pricing.get("allPassengerDisplayTaxTotal", {}).get("amount", 0.0)
                cash_total = slice_pricing.get("allPassengerDisplayTotal", {}).get("amount", 0.0)
                
                if search_type == "Award":
                    cpp = calculate_cpp(cash_total, taxes_fees, points)
                else:
                    cpp = 0.0
                
                flight = {
                    "is_nonstop": is_nonstop,
                    "segments": parsed_segments,
                    "total_duration": duration_str,
                    "points_required": points,
                    "cash_price_usd": cash_total,
                    "taxes_fees_usd": taxes_fees,
                    "cpp": cpp,
                    "_product_type": product_type
                }
                
                flights.append(flight)
        
        return flights

# ============================================================================
# Helper Functions
# ============================================================================

def format_duration(minutes: int) -> str:
    """Convert minutes to 'Xh Ym' format"""
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m"

def calculate_cpp(cash_price: float, taxes_fees: float, points: int) -> float:
    """Calculate cents per point (CPP)"""
    if points == 0:
        return 0.0
    return round((cash_price - taxes_fees) / points * 100, 2)

def format_time(datetime_str: str) -> str:
    """Extract time from ISO datetime string (HH:MM format)"""
    if "T" not in datetime_str:
        return ""
    time_part = datetime_str.split("T")[1]
    return time_part[:5]

# ============================================================================
# Main Scraper Function
# ============================================================================

async def scrape_flights(
    origin: str,
    destination: str,
    date: str,
    passengers: int,
    cookie_manager: CookieManager,
    cabin_filter: str = "COACH",
    search_types: List[str] = ["Award", "Revenue"],
    rate_limit: float = 1.0
) -> Tuple[Dict[str, Optional[List[Dict[str, Any]]]], Dict[str, Optional[Dict[str, Any]]]]:
    """Main scraping function with enhanced error handling"""
    
    rate_limiter = AdaptiveRateLimiter(rate=rate_limit, burst=int(rate_limit * 2))
    client = AAFlightClient(cookie_manager, rate_limiter)
    
    results = {}
    raw_responses = {}
    
    for search_type in search_types:
        logger.info(f"Starting {search_type} search...")
        
        api_response = await client.search_flights(
            origin, destination, date, passengers, search_type
        )
        
        raw_responses[search_type] = api_response
        
        if not api_response:
            logger.warning(f"⚠️ {search_type} search returned no data")
            results[search_type] = None
            continue
        
        flights = FlightDataParser.parse_flight_options(
            api_response,
            cabin_filter=cabin_filter,
            search_type=search_type
        )
        
        if not flights:
            logger.warning(f"⚠️ No {cabin_filter} flights found in {search_type} response")
            results[search_type] = None
            continue
        
        logger.success(f"✓ Found {len(flights)} {search_type} flights")
        results[search_type] = flights
    
    return results, raw_responses

# ============================================================================
# Storage (unchanged)
# ============================================================================

def save_results(
    results: Dict[str, Optional[List[Dict[str, Any]]]],
    raw_responses: Dict[str, Optional[Dict[str, Any]]],
    output_dir: Path,
    origin: str,
    destination: str,
    date: str,
    passengers: int,  # <-- ADD THIS PARAMETER
    cabin_filter: str
):
    """Save scraping results"""
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw_data"
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_filename = f"{origin}_{destination}_{date}_{timestamp}"
    
    # Save raw responses
    for search_type, raw_data in raw_responses.items():
        if raw_data is not None:
            raw_file = raw_dir / f"{base_filename}_{search_type.lower()}_raw.json"
            raw_file.write_text(json.dumps(raw_data, ensure_ascii=False, indent=2))
            logger.info(f"💾 Saved raw {search_type} response: {raw_file.name}")
    
    # Merge results
    award_flights = results.get("Award")
    revenue_flights = results.get("Revenue")
    merged_flights = []
    
    if award_flights and revenue_flights:
        revenue_lookup = {}
        for flight in revenue_flights:
            if flight.get("_product_type") != "COACH":
                continue
            cash_price = flight.get("cash_price_usd", 0.0)
            if cash_price <= 0:
                continue
            
            segments = flight["segments"]
            if segments:
                dep_time = segments[0]["departure_time"]
                arr_time = segments[-1]["arrival_time"]
                nonstop = flight["is_nonstop"]
                key = (dep_time, arr_time, nonstop)
                revenue_lookup[key] = flight
        
        logger.info(f"Found {len(revenue_lookup)} valid revenue flights")
        
        for award_flight in award_flights:
            if award_flight.get("_product_type") != "COACH":
                continue
            
            segments = award_flight["segments"]
            if segments:
                dep_time = segments[0]["departure_time"]
                arr_time = segments[-1]["arrival_time"]
                nonstop = award_flight["is_nonstop"]
                key = (dep_time, arr_time, nonstop)
                
                if key in revenue_lookup:
                    revenue_flight = revenue_lookup[key]
                    merged_flight = award_flight.copy()
                    merged_flight["cash_price_usd"] = revenue_flight["cash_price_usd"]
                    
                    if merged_flight["points_required"] > 0:
                        merged_flight["cpp"] = calculate_cpp(
                            revenue_flight["cash_price_usd"],
                            award_flight["taxes_fees_usd"],
                            merged_flight["points_required"]
                        )
                    
                    merged_flight.pop("_product_type", None)
                    merged_flights.append(merged_flight)
    
    elif not award_flights or not revenue_flights:
        logger.warning("⚠️ Cannot merge - missing Award or Revenue data")
    
    # Save merged results
    merged_result = {
        "search_metadata": {
            "origin": origin.upper(),
            "destination": destination.upper(),
            "date": date,
            "passengers": passengers,  # <-- NOW THIS WORKS
            "cabin_class": CABIN_CLASS_MAP.get(cabin_filter, cabin_filter.lower())
        },
        "flights": merged_flights,
        "total_results": len(merged_flights)
    }
    
    output_file = output_dir / f"{base_filename}_combined.json"
    output_file.write_text(json.dumps(merged_result, ensure_ascii=False, indent=2))
    
    if merged_flights:
        logger.success(f"💾 Saved {len(merged_flights)} merged flights: {output_file.name}")
    else:
        logger.warning(f"⚠️ Saved empty results: {output_file.name}")
    
    # Save merged results
    merged_result = {
        "search_metadata": {
            "origin": origin.upper(),
            "destination": destination.upper(),
            "date": date,
            "passengers": passengers,
            "cabin_class": CABIN_CLASS_MAP.get(cabin_filter, cabin_filter.lower())
        },
        "flights": merged_flights,
        "total_results": len(merged_flights)
    }
    
    output_file = output_dir / f"{base_filename}_combined.json"
    output_file.write_text(json.dumps(merged_result, ensure_ascii=False, indent=2))
    
    if merged_flights:
        logger.success(f"💾 Saved {len(merged_flights)} merged flights: {output_file.name}")
    else:
        logger.warning(f"⚠️ Saved empty results: {output_file.name}")

# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="American Airlines Flight Scraper - Production Ready",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    # Cookie management
    cookie_group = parser.add_argument_group("Cookie Management")
    cookie_group.add_argument("--extract-cookies", action="store_true", help="Extract fresh cookies")
    cookie_group.add_argument("--cookies", type=str, help=f"Cookie file (default: {DEFAULT_COOKIE_FILE})")
    cookie_group.add_argument("--no-headless", action="store_true", help="Visible browser mode")
    cookie_group.add_argument("--cookies-only", action="store_true", help="Extract cookies only, no search")
    cookie_group.add_argument("--cookie-wait-time", type=int, default=15, help="Cookie extraction wait time")
    cookie_group.add_argument("--test-origin", type=str, default="SRQ", help="Test origin for cookies")
    cookie_group.add_argument("--test-destination", type=str, default="BFL", help="Test destination for cookies")
    cookie_group.add_argument("--test-days-ahead", type=int, default=7, help="Test date offset")
    
    # Flight search
    search_group = parser.add_argument_group("Flight Search")
    search_group.add_argument("--origin", type=str, help="Origin airport code")
    search_group.add_argument("--destination", type=str, help="Destination airport code")
    search_group.add_argument("--date", type=str, help="Departure date (YYYY-MM-DD)")
    search_group.add_argument("--passengers", type=int, default=1, help="Number of passengers")
    search_group.add_argument(
        "--cabin",
        type=str,
        default="COACH",
        choices=["COACH", "BUSINESS", "FIRST", "PREMIUM_ECONOMY"],
        help="Cabin class"
    )
    search_group.add_argument(
        "--search-type",
        type=str,
        nargs="+",
        default=["Award", "Revenue"],
        choices=["Award", "Revenue"],
        help="Search types"
    )
    
    # Configuration
    config_group = parser.add_argument_group("Configuration")
    config_group.add_argument("--output", type=str, default="./output", help="Output directory")
    config_group.add_argument("--rate-limit", type=float, default=1.0, help="Requests per second")
    config_group.add_argument("--verbose", action="store_true", help="Debug logging")
    config_group.add_argument("--log-file", type=str, help="Log file path")
    
    args = parser.parse_args()
    
    # Setup logging
    log_file = Path(args.log_file) if args.log_file else Path("./logs/aa_scraper.log")
    setup_logging(verbose=args.verbose, log_file=log_file)
    
    logger.info("="*60)
    logger.info("AA Flight Scraper - Production Ready")
    logger.info("="*60)
    
    # Cookie file path
    cookie_file = Path(args.cookies) if args.cookies else DEFAULT_COOKIE_FILE
    
    # Initialize cookie manager
    cookie_manager = CookieManager(
        cookie_file=cookie_file,
        test_origin=args.test_origin,
        test_destination=args.test_destination,
        test_days_ahead=args.test_days_ahead
    )
    
    async def run():
        try:
            # Handle cookies-only mode
            if args.cookies_only:
                if not args.extract_cookies:
                    logger.error("--cookies-only requires --extract-cookies")
                    sys.exit(1)
                
                await cookie_manager.get_cookies(
                    force_refresh=True,
                    headless=not args.no_headless,
                    wait_time=args.cookie_wait_time
                )
                logger.success("Cookie extraction complete!")
                return
            
            # Validate search parameters
            if not all([args.origin, args.destination, args.date]):
                logger.error("Missing required: --origin, --destination, --date")
                sys.exit(1)
            
            # Extract cookies if requested
            if args.extract_cookies:
                await cookie_manager.get_cookies(
                    force_refresh=True,
                    headless=not args.no_headless,
                    wait_time=args.cookie_wait_time
                )
            
            # Search flights
            logger.info(f"Searching flights: {args.origin} → {args.destination} on {args.date}")
            
            results, raw_responses = await scrape_flights(
                origin=args.origin.upper(),
                destination=args.destination.upper(),
                date=args.date,
                passengers=args.passengers,
                cookie_manager=cookie_manager,
                cabin_filter=args.cabin,
                search_types=args.search_type,
                rate_limit=args.rate_limit
            )
            
            # Check results
            if not any(results.values()):
                logger.error("All searches failed")
                sys.exit(1)
            
            # Save results
            output_dir = Path(args.output)
            save_results(
                results,
                raw_responses,
                output_dir,
                args.origin,
                args.destination,
                args.date,
                args.passengers,
                args.cabin
            )
            
            # Summary
            logger.info("")
            logger.info("="*60)
            logger.success("✓ Scraping complete!")
            logger.info(f"  Route: {args.origin} → {args.destination}")
            logger.info(f"  Date: {args.date}")
            logger.info(f"  Cabin: {CABIN_CLASS_MAP.get(args.cabin, args.cabin.lower())}")
            
            for search_type, result in results.items():
                if result:
                    logger.info(f"  {search_type}: {len(result)} flights")
            
            logger.info(f"  Output: {output_dir}")
            logger.info("="*60)
            
        except KeyboardInterrupt:
            logger.warning("Interrupted by user")
            sys.exit(1)
        except Exception as e:
            logger.exception(f"Fatal error: {e}")
            sys.exit(1)
    
    asyncio.run(run())

if __name__ == "__main__":
    main()