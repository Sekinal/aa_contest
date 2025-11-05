#!/usr/bin/env python3
"""
American Airlines Flight Scraper - Operation Point Break
Production-ready async scraper with bot evasion and data storage
Supports both Award (points) and Revenue (cash) searches
Features automatic cookie extraction via Camoufox
"""


import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional


import httpx


# ============================================================================
# Configuration
# ============================================================================


API_ENDPOINT = "https://www.aa.com/booking/api/search/itinerary"
BASE_URL = "https://www.aa.com"
DEFAULT_COOKIE_FILE = Path("./cookies/aa_cookies.json")


# Realistic Firefox headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


# Cabin class mapping
CABIN_CLASS_MAP = {
    "COACH": "economy",
    "BUSINESS": "business",
    "FIRST": "first",
    "PREMIUM_ECONOMY": "premium_economy"
}


# ============================================================================
# Logging Setup
# ============================================================================


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


# ============================================================================
# Cookie Extraction with Camoufox
# ============================================================================

def clean_captured_headers(captured_headers: Dict[str, str]) -> Dict[str, str]:
    """
    Clean captured headers for use with httpx.
    Removes problematic headers that httpx handles automatically.
    """
    # Headers that httpx sets automatically and should NOT be copied
    SKIP_HEADERS = {
        'host',           # httpx sets from URL
        'content-length', # httpx calculates
        'connection',     # httpx manages
        'cookie',         # We handle separately
        'accept-encoding', # httpx sets based on available decoders
    }
    
    # HTTP/2 pseudo-headers - never include these
    PSEUDO_HEADERS = {':method', ':path', ':scheme', ':authority', ':status'}
    
    cleaned = {}
    for key, value in captured_headers.items():
        key_lower = key.lower()
        
        # Skip pseudo-headers
        if key_lower.startswith(':'):
            continue
            
        # Skip headers httpx handles
        if key_lower in SKIP_HEADERS:
            continue
            
        # Keep everything else
        cleaned[key] = value
    
    return cleaned

async def extract_cookies_with_camoufox(
    headless: bool = True,
    wait_time: int = 15,
    origin: str = "SRQ",
    destination: str = "BFL",
    days_ahead: int = 7
) -> tuple[Dict[str, str], Dict[str, str], str]:
    """
    Extract cookies AND request headers from AA.com using Camoufox.
    
    Returns:
        Tuple of (cookies, headers, referer_url)
    """
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        logger.error("❌ Camoufox not installed. Install with: pip install camoufox")
        sys.exit(1)
    
    from datetime import datetime, timedelta
    
    departure_date = (datetime.now() + timedelta(days=days_ahead)).strftime("%m/%d/%Y")
    
    logger.info("🦊 Launching Camoufox browser for cookie extraction...")
    logger.info(f"   Headless mode: {headless}")
    logger.info(f"   Test search: {origin} → {destination}")
    logger.info(f"   Departure: {departure_date}")
    
    cookies = {}
    captured_headers = {}
    referer_url = ""
    
    try:
        async with AsyncCamoufox(headless=headless) as browser:
            page = await browser.new_page()
            
            # Set up request interception to capture API call headers
            api_request_captured = False
            
            async def handle_request(route):
                request = route.request
                
                if "/booking/api/search/itinerary" in request.url:
                    nonlocal api_request_captured, captured_headers
                    
                    if not api_request_captured:
                        logger.info("🎯 Intercepted browser API call - capturing headers...")
                        
                        # Get raw headers
                        raw_headers = {}
                        for key, value in request.headers.items():
                            raw_headers[key] = value
                        
                        # Clean them for httpx
                        captured_headers = clean_captured_headers(raw_headers)
                        
                        api_request_captured = True
                        logger.info(f"   Captured {len(captured_headers)} headers from browser")
                        logger.debug(f"   Headers: {list(captured_headers.keys())}")
                
                await route.continue_()
            
            # Enable request interception
            await page.route("**/*", handle_request)
            
            # Step 1: Visit homepage
            logger.info(f"🌐 Step 1/4: Navigating to homepage...")
            await page.goto(f"{BASE_URL}/?locale=en_US", wait_until="networkidle")
            await page.wait_for_timeout(3000)
            
            # Handle cookie consent
            try:
                accept_btn = await page.query_selector('#onetrust-accept-btn-handler, #accept-recommended-btn-handler')
                if accept_btn:
                    await accept_btn.click()
                    logger.info("✓ Accepted cookie consent")
                    await page.wait_for_timeout(1000)
            except:
                pass
            
            # Step 2: Fill out search form
            logger.info(f"📝 Step 2/4: Filling out search form...")
            
            try:
                await page.wait_for_selector('input[name="originAirport"]', timeout=10000, state="visible")
                await page.wait_for_timeout(2000)
                
                # Verify one-way is selected
                one_way_checked = await page.is_checked('input[id="flightSearchForm.tripType.oneWay"]')
                if not one_way_checked:
                    await page.click('label[for="flightSearchForm.tripType.oneWay"]')
                    await page.wait_for_timeout(500)
                logger.info("✓ One-way trip selected")
                
                # Fill origin
                origin_input = await page.query_selector('input[name="originAirport"]')
                await origin_input.click()
                await page.wait_for_timeout(500)
                
                await page.evaluate(f'''
                    const originInput = document.querySelector('input[name="originAirport"]');
                    originInput.value = "{origin}";
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
                    destInput.value = "{destination}";
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
                
                logger.info(f"✓ Form filled: {origin} → {destination} on {departure_date}")
                
            except Exception as e:
                logger.error(f"❌ Failed to fill form: {e}")
                await page.screenshot(path="error_form_fill.png")
                raise
            
            # Step 3: Submit search
            logger.info(f"🔍 Step 3/4: Submitting flight search...")
            try:
                submit_btn = await page.query_selector('input[type="submit"][id="flightSearchForm.button.reSubmit"]')
                if not submit_btn:
                    raise Exception("Submit button not found")
                
                try:
                    async with page.expect_navigation(wait_until="networkidle", timeout=60000):
                        await submit_btn.click()
                        logger.info("✓ Search button clicked, waiting for navigation...")
                except Exception as nav_error:
                    logger.warning(f"⚠️ Navigation wait failed: {nav_error}")
                
                await page.wait_for_timeout(5000)
                
                current_url = page.url
                referer_url = current_url  # Capture the actual results page URL
                logger.info(f"📍 Current URL: {current_url}")
                
                if any(pattern in current_url for pattern in ["/choose-flights", "/flights", "/booking/find-flights"]):
                    logger.info("✅ Successfully reached flight results page!")
                elif "homePage" in current_url:
                    logger.error("❌ Form submission failed")
                    return {}, {}, ""
                else:
                    logger.warning(f"⚠️ Unexpected URL: {current_url}")
                
            except Exception as e:
                logger.error(f"❌ Failed to submit search: {e}")
                await page.screenshot(path="error_submit.png")
                raise
            
            # Step 4: Wait for results (this will trigger the API call we're intercepting)
            logger.info(f"⏳ Step 4/4: Waiting {wait_time}s for results and API calls...")
            await page.wait_for_timeout(wait_time * 1000)
            
            # Check if we captured the API request
            if not api_request_captured:
                logger.warning("⚠️ Did not intercept API call - may need to trigger it manually")
                # Try scrolling or interacting to trigger API calls
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(3000)
            
            # Extract cookies
            raw_cookies = await page.context.cookies()
            for cookie in raw_cookies:
                cookies[cookie['name']] = cookie['value']
            
            logger.info(f"✓ Extracted {len(cookies)} cookies from search session")
            
            # Validate
            critical_cookies = ['XSRF-TOKEN', 'spa_session_id', 'JSESSIONID']
            found_critical = [c for c in critical_cookies if c in cookies]
            
            if found_critical:
                logger.info(f"  ✓ Found critical cookies: {', '.join(found_critical)}")
            
            bot_cookies = ['_abck', 'bm_sz', 'ak_bmsc', 'bm_sv']
            found_bot = [c for c in bot_cookies if c in cookies]
            if found_bot:
                logger.info(f"  ✓ Found bot-defense cookies: {', '.join(found_bot)}")
            
            # Return cookies, headers, and referer
            return cookies, captured_headers, referer_url
            
    except Exception as e:
        logger.error(f"❌ Cookie extraction failed: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return {}, {}, ""


def save_cookies_to_file(cookies: Dict[str, str], cookie_file: Path):
    """Save cookies to a JSON file"""
    ensure_directory(cookie_file.parent)
    
    cookie_file.write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    logger.info(f"💾 Saved cookies to: {cookie_file}")


def load_cookies_from_file(cookie_file: Path) -> Dict[str, str]:
    """Load cookies from a file (JSON format)"""
    cookies = {}
    
    if not cookie_file.exists():
        logger.error(f"❌ Cookie file not found: {cookie_file}")
        return cookies
    
    content = cookie_file.read_text(encoding="utf-8").strip()
    
    try:
        cookies = json.loads(content)
        logger.info(f"📂 Loaded {len(cookies)} cookies from file: {cookie_file}")
        
        # Check age of cookies
        try:
            mtime = cookie_file.stat().st_mtime
            age_hours = (datetime.now().timestamp() - mtime) / 3600
            if age_hours > 24:
                logger.warning(f"⚠️ Cookies are {age_hours:.1f} hours old - may be expired")
            else:
                logger.info(f"  Cookies age: {age_hours:.1f} hours")
        except:
            pass
            
        return cookies
    except json.JSONDecodeError:
        logger.error("❌ Failed to parse cookie file as JSON")
        return {}


async def get_cookies(
    extract: bool = False,
    cookie_file: Optional[Path] = None,
    headless: bool = True,
    auto_save: bool = True,
    wait_time: int = 15,
    test_origin: str = "SRQ",
    test_destination: str = "BFL",
    test_days_ahead: int = 7
) -> tuple[Dict[str, str], Dict[str, str], str]:
    """
    Get cookies, headers, and referer URL
    
    Returns:
        Tuple of (cookies, headers, referer_url)
    """
    if extract:
        cookies, headers, referer = await extract_cookies_with_camoufox(
            headless=headless,
            wait_time=wait_time,
            origin=test_origin,
            destination=test_destination,
            days_ahead=test_days_ahead
        )
        
        if not cookies:
            logger.error("❌ Cookie extraction failed")
            return {}, {}, ""
        
        # Auto-save if requested
        if auto_save and cookie_file:
            save_cookies_to_file(cookies, cookie_file)
            
            # Also save headers
            if headers:
                headers_file = cookie_file.parent / f"{cookie_file.stem}_headers.json"
                headers_file.write_text(json.dumps(headers, indent=2))
                logger.info(f"💾 Saved headers to: {headers_file}")
            
            # Save referer
            if referer:
                referer_file = cookie_file.parent / f"{cookie_file.stem}_referer.txt"
                referer_file.write_text(referer)
                logger.info(f"💾 Saved referer to: {referer_file}")
        
        return cookies, headers, referer
    
    elif cookie_file and cookie_file.exists():
        cookies = load_cookies_from_file(cookie_file)
        
        # Try to load headers
        headers_file = cookie_file.parent / f"{cookie_file.stem}_headers.json"
        headers = {}
        if headers_file.exists():
            try:
                headers = json.loads(headers_file.read_text())
                logger.info(f"📂 Loaded {len(headers)} headers from: {headers_file}")
            except:
                pass
        
        # Try to load referer
        referer_file = cookie_file.parent / f"{cookie_file.stem}_referer.txt"
        referer = ""
        if referer_file.exists():
            referer = referer_file.read_text().strip()
            logger.info(f"📂 Loaded referer from: {referer_file}")
        
        return cookies, headers, referer
    
    else:
        logger.error("❌ No cookie source specified")
        return {}, {}, ""

# ============================================================================
# Data Models
# ============================================================================


@dataclass
class FlightSegment:
    """Single flight segment/leg"""
    flight_number: str
    departure_time: str
    arrival_time: str


@dataclass
class FlightOption:
    """Complete flight option with pricing"""
    is_nonstop: bool
    segments: List[Dict[str, str]]  # List of segment dicts
    total_duration: str
    points_required: int
    cash_price_usd: float
    taxes_fees_usd: float
    cpp: float


@dataclass
class SearchMetadata:
    """Search parameters"""
    origin: str
    destination: str
    date: str
    passengers: int
    cabin_class: str  # Changed from search_type


# ============================================================================
# Helper Functions
# ============================================================================


def utc_timestamp() -> str:
    """Get current UTC timestamp"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_directory(path: Path):
    """Create directory if it doesn't exist"""
    path.mkdir(parents=True, exist_ok=True)


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
    return time_part[:5]  # HH:MM


# ============================================================================
# Rate Limiting
# ============================================================================


class RateLimiter:
    """Token bucket rate limiter"""
    
    def __init__(self, rate: float = 2.0, burst: int = 5):
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.last_update = asyncio.get_event_loop().time()
        self.lock = asyncio.Lock()
    
    async def acquire(self):
        """Acquire a token (wait if necessary)"""
        async with self.lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self.last_update
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_update = now
            
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            
            wait_time = (1.0 - self.tokens) / self.rate
            await asyncio.sleep(wait_time)
            self.tokens = 0.0


# ============================================================================
# API Client
# ============================================================================


class AAFlightClient:
    """American Airlines flight search client"""
    
    def __init__(
        self,
        cookies: Dict[str, str],
        rate_limiter: RateLimiter,
        timeout: float = 30.0,
        captured_headers: Optional[Dict[str, str]] = None,
        referer_url: Optional[str] = None
    ):
        self.cookies = cookies
        self.rate_limiter = rate_limiter
        self.timeout = timeout
        
        # Define proper header order (bot detection systems check this!)
        HEADER_ORDER = [
            'user-agent',       # Make sure this comes first!
            'accept',
            'accept-language',
            'content-type',
            'referer',
            'x-xsrf-token',
            'x-cid',
            'origin',
            'sec-fetch-dest',
            'sec-fetch-mode',
            'sec-fetch-site',
            'priority',
            'te',
        ]
        
        # Build headers in correct order
        if captured_headers:
            logger.info("🎯 Using captured browser headers for requests")
            
            # Create case-insensitive lookup of captured headers
            captured_lower = {k.lower(): (k, v) for k, v in captured_headers.items()}
            
            # Start with ordered headers (use original case from browser)
            ordered_headers = {}
            for header_name in HEADER_ORDER:
                if header_name in captured_lower:
                    original_key, value = captured_lower[header_name]
                    ordered_headers[original_key] = value
            
            # Add any remaining captured headers not in our order list
            for key, value in captured_headers.items():
                if key.lower() not in [h.lower() for h in ordered_headers.keys()]:
                    ordered_headers[key] = value
            
            self.headers = ordered_headers
            
            # Override referer if we have the actual results page URL
            if referer_url:
                # Find the existing referer key (case-insensitive)
                referer_key = None
                for key in self.headers.keys():
                    if key.lower() == 'referer':
                        referer_key = key
                        break
                if referer_key:
                    self.headers[referer_key] = referer_url
                else:
                    self.headers['Referer'] = referer_url
                logger.info(f"   Using session Referer: {referer_url}")
            
        else:
            logger.warning("⚠️ No captured headers - using default headers")
            self.headers = HEADERS.copy()
            
            # Extract from cookies
            if "XSRF-TOKEN" in cookies:
                self.headers["X-XSRF-TOKEN"] = cookies["XSRF-TOKEN"]
            if "spa_session_id" in cookies:
                self.headers["X-CID"] = cookies["spa_session_id"]
            if referer_url:
                self.headers["Referer"] = referer_url
        
        # Ensure critical headers are present (case-insensitive check)
        headers_lower = {k.lower(): k for k in self.headers.keys()}
        
        if 'x-xsrf-token' not in headers_lower and "XSRF-TOKEN" in cookies:
            self.headers["X-XSRF-TOKEN"] = cookies["XSRF-TOKEN"]
            logger.debug("Added X-XSRF-TOKEN from cookies")
        
        if 'x-cid' not in headers_lower and "spa_session_id" in cookies:
            self.headers["X-CID"] = cookies["spa_session_id"]
            logger.debug("Added X-CID from cookies")
        
        # CRITICAL: Ensure User-Agent is set (httpx uses its own if not in headers)
        if 'user-agent' not in headers_lower:
            self.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0"
            logger.warning("⚠️ User-Agent was missing - added Firefox UA")
        
        logger.debug(f"Final headers: {list(self.headers.keys())}")
        logger.debug(f"Header values: {self.headers}")
    
    def _build_request_payload(
        self,
        origin: str,
        destination: str,
        date: str,
        passengers: int = 1,
        search_type: str = "Award"
    ) -> Dict[str, Any]:
        """Build the API request payload"""
        
        # Base payload structure
        payload = {
            "metadata": {
                "selectedProducts": [],
                "tripType": "OneWay",
                "udo": {}
            },
            "passengers": [
                {
                    "type": "adult",
                    "count": passengers
                }
            ],
            "requestHeader": {
                "clientId": "AAcom"
            },
            "slices": [
                {
                    "allCarriers": True,
                    "cabin": "",
                    "connectionCity": None,
                    "departureDate": date,
                    "destination": destination,
                    "destinationNearbyAirports": False,
                    "maxStops": None,
                    "origin": origin,
                    "originNearbyAirports": False
                }
            ],
            "tripOptions": {
                "corporateBooking": False,
                "fareType": "Lowest",
                "locale": "en_US",
                "pointOfSale": "",
                "searchType": search_type  # "Award" or "Revenue"
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
        
        # Add search_method for Revenue searches
        if search_type == "Revenue":
            payload["metadata"]["udo"]["search_method"] = "Lowest"
        
        return payload
    
    async def search_flights(
        self,
        origin: str,
        destination: str,
        date: str,
        passengers: int,
        search_type: str = "Award",
        cabin_filter: str = "COACH"
    ) -> Optional[List[Dict[str, Any]]]:
        """Search for flights"""
        
        await self.rate_limiter.acquire()
        
        payload = self._build_request_payload(
            origin, destination, date, passengers, search_type
        )
        
        # Try HTTP/2 first, fallback to HTTP/1.1
        for http_version in ["HTTP/2", "HTTP/1.1"]:
            try:
                # Configure httpx with specific HTTP version
                limits = httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10,
                    keepalive_expiry=30.0
                )
                
                # Use HTTP/1.1 if HTTP/2 fails
                http2_enabled = (http_version == "HTTP/2")
                
                async with httpx.AsyncClient(
                    cookies=self.cookies,
                    headers=self.headers,
                    timeout=self.timeout,
                    limits=limits,
                    http2=http2_enabled,
                    follow_redirects=True
                ) as client:
                    logger.info(f"🔍 Searching {search_type} flights: {origin} → {destination} on {date} (via {http_version})")
                    
                    response = await client.post(
                        f"{BASE_URL}/booking/api/search/itinerary",
                        json=payload
                    )
                    
                    # Log the actual request details in debug mode
                    logger.debug(f"Request URL: {response.request.url}")
                    logger.debug(f"Request headers: {dict(response.request.headers)}")
                    logger.debug(f"Response status: {response.status_code}")
                    
                    if response.status_code == 403:
                        logger.error(f"❌ 403 Forbidden - Bot detection triggered (via {http_version})")
                        if http_version == "HTTP/2":
                            logger.info("   Retrying with HTTP/1.1...")
                            continue  # Try HTTP/1.1
                        else:
                            logger.error("   This typically means:")
                            logger.error("   1. Cookies expired or invalid")
                            logger.error("   2. Missing critical session cookies")
                            logger.error("   3. Bot detection system flagged the request")
                            logger.error("")
                            logger.error("   Try:")
                            logger.error("   - Run with --extract-cookies to get fresh cookies")
                            logger.error("   - Increase --cookie-wait-time (default: 15s)")
                            logger.error("   - Run with --no-headless to see what's happening")
                            logger.error("   - Check if cookies file has 'spa_session_id'")
                            return None
                    
                    response.raise_for_status()

                    data = response.json()
                    logger.info(f"✅ {search_type} search successful via HTTP/2")
                    return data  # Return raw JSON, let scrape_flights() parse it
                    
            except httpx.StreamError as e:
                logger.error(f"❌ Stream error with {http_version}: {e}")
                if http_version == "HTTP/2":
                    logger.info("   Retrying with HTTP/1.1...")
                    continue  # Try HTTP/1.1
                else:
                    return None
                    
            except httpx.HTTPStatusError as e:
                logger.error(f"❌ HTTP {e.response.status_code}: {e}")
                if http_version == "HTTP/2" and e.response.status_code in [403, 421]:
                    logger.info("   Retrying with HTTP/1.1...")
                    continue
                return None
                
            except Exception as e:
                logger.error(f"❌ Request failed with {http_version}: {e}")
                if http_version == "HTTP/2":
                    logger.info("   Retrying with HTTP/1.1...")
                    continue
                return None
        
        return None


# ============================================================================
# Data Parser
# ============================================================================


class FlightDataParser:
    """Parse AA API response into structured data"""
    
    @staticmethod
    def parse_flight_options(
        api_response: Dict[str, Any],
        cabin_filter: str = "COACH",
        search_type: str = "Award"
    ) -> List[Dict[str, Any]]:
        """
        Parse flight options from API response.
        Filters for specified cabin class.
        Returns list of flight dicts (not dataclass instances).
        """
        flights = []
        slices = api_response.get("slices", [])
        
        for slice_data in slices:
            # Extract flight metadata
            duration_min = slice_data.get("durationInMinutes", 0)
            duration_str = format_duration(duration_min)
            is_nonstop = slice_data.get("stops", 0) == 0
            
            # Parse segments - extract actual flight numbers
            segments_data = slice_data.get("segments", [])
            parsed_segments = []
            
            for segment in segments_data:
                # Get flight info
                flight_info = segment.get("flight", {})
                carrier_code = flight_info.get("carrierCode", "")
                flight_num = flight_info.get("flightNumber", "")
                flight_number = f"{carrier_code}{flight_num}"
                
                # Get times
                dep_time = format_time(segment.get("departureDateTime", ""))
                arr_time = format_time(segment.get("arrivalDateTime", ""))
                
                parsed_segments.append({
                    "flight_number": flight_number,
                    "departure_time": dep_time,
                    "arrival_time": arr_time
                })
            
            if not parsed_segments:
                continue
            
            # Iterate through product pricing options
            pricing_detail = slice_data.get("pricingDetail", [])
            
            for pricing_option in pricing_detail:
                # Check if product is available
                if not pricing_option.get("productAvailable", False):
                    continue
                
                # Get product type
                product_type = pricing_option.get("productType", "")
                
                # Filter by cabin class (exact match on product_type "COACH")
                if cabin_filter == "COACH" and product_type != "COACH":
                    continue
                elif cabin_filter != "COACH" and not product_type.startswith(cabin_filter):
                    continue
                
                # Extract pricing information from slicePricing
                slice_pricing = pricing_option.get("slicePricing", {})
                
                if not slice_pricing:
                    continue
                
                # Points required (for Award searches, this will be actual points)
                # For Revenue searches, perPassengerAwardPoints contains fare amount as string
                points_str = slice_pricing.get("perPassengerAwardPoints", "0")
                if isinstance(points_str, str):
                    points_or_fare = float(points_str.replace(",", ""))
                else:
                    points_or_fare = float(points_str)
                
                # For Award: points_or_fare is points, for Revenue: it's base fare
                if search_type == "Award":
                    points = int(points_or_fare)
                else:
                    points = 0
                
                # Taxes and fees
                taxes_fees = slice_pricing.get("allPassengerDisplayTaxTotal", {}).get("amount", 0.0)
                
                # Total price
                cash_total = slice_pricing.get("allPassengerDisplayTotal", {}).get("amount", 0.0)
                
                # Calculate CPP (only meaningful for Award)
                if search_type == "Award":
                    cpp = calculate_cpp(cash_total, taxes_fees, points)
                else:
                    cpp = 0.0
                
                # Build flight dict (without cabin_class and product_type)
                flight = {
                    "is_nonstop": is_nonstop,
                    "segments": parsed_segments,
                    "total_duration": duration_str,
                    "points_required": points,
                    "cash_price_usd": cash_total,
                    "taxes_fees_usd": taxes_fees,
                    "cpp": cpp,
                    # Store product_type temporarily for merging logic
                    "_product_type": product_type
                }
                
                flights.append(flight)
        
        return flights


# ============================================================================
# Main Scraper
# ============================================================================


async def scrape_flights(
    origin: str,
    destination: str,
    date: str,
    passengers: int,
    cookies: Dict[str, str],
    cabin_filter: str = "COACH",
    search_types: List[str] = ["Award", "Revenue"],
    rate_limit: float = 1.0,
    captured_headers: Optional[Dict[str, str]] = None,
    referer_url: Optional[str] = None
) -> tuple[Dict[str, Optional[List[Dict[str, Any]]]], Dict[str, Optional[Dict[str, Any]]]]:
    """Main scraping function"""
    rate_limiter = RateLimiter(rate=rate_limit, burst=int(rate_limit * 2))
    client = AAFlightClient(
        cookies, 
        rate_limiter,
        captured_headers=captured_headers,
        referer_url=referer_url
    )
    
    results = {}
    raw_responses = {}
    
    for search_type in search_types:
        # Search flights
        api_response = await client.search_flights(
            origin, destination, date, passengers, search_type
        )
        
        # Store raw response
        raw_responses[search_type] = api_response
        
        if not api_response:
            results[search_type] = None
            continue
        
        # Parse response
        flights = FlightDataParser.parse_flight_options(
            api_response, 
            cabin_filter=cabin_filter,
            search_type=search_type
        )
        
        if not flights:
            logger.warning(f"⚠️ No {cabin_filter} flights found in {search_type} response")
            results[search_type] = None
            continue
        
        results[search_type] = flights
    
    return results, raw_responses


# ============================================================================
# Storage
# ============================================================================


def save_results(
    results: Dict[str, Optional[List[Dict[str, Any]]]],
    raw_responses: Dict[str, Optional[Dict[str, Any]]],
    output_dir: Path,
    origin: str,
    destination: str,
    date: str,
    cabin_filter: str
):
    """Save scraping results with Award and Revenue data properly merged, plus raw responses"""
    ensure_directory(output_dir)
    
    # Create raw data subdirectory
    raw_dir = output_dir / "raw_data"
    ensure_directory(raw_dir)
    
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_filename = f"{origin}_{destination}_{date}_{timestamp}"
    
    # ========================================================================
    # Save raw API responses
    # ========================================================================
    for search_type, raw_data in raw_responses.items():
        if raw_data is not None:
            raw_file = raw_dir / f"{base_filename}_{search_type.lower()}_raw.json"
            raw_file.write_text(
                json.dumps(raw_data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            logger.info(f"✓ Saved raw {search_type} response: {raw_file}")
    
    # ========================================================================
    # Process and save merged results (ONLY if we have both Award and Revenue)
    # ========================================================================
    
    award_flights = results.get("Award")
    revenue_flights = results.get("Revenue")
    
    # Create a merged flight list
    merged_flights = []
    
    if award_flights and revenue_flights:
        # Create a lookup dict for revenue flights - ONLY exact "COACH" product type
        revenue_lookup = {}
        for flight in revenue_flights:
            # Filter: Only include exact "COACH" product type
            if flight.get("_product_type") != "COACH":
                continue
            
            # Skip if no valid cash price (must be > 0)
            cash_price = flight.get("cash_price_usd", 0.0)
            if cash_price <= 0:
                logger.debug(f"Skipping revenue flight with invalid cash_price_usd: {cash_price}")
                continue
                
            # Create key based on segments
            segments = flight["segments"]
            if not segments:
                continue
            
            dep_time = segments[0]["departure_time"]
            arr_time = segments[-1]["arrival_time"]
            nonstop = flight["is_nonstop"]
            key = (dep_time, arr_time, nonstop)
            
            if key not in revenue_lookup:
                revenue_lookup[key] = flight
        
        logger.info(f"📊 Found {len(revenue_lookup)} valid revenue flights with pricing")
        
        # Merge Award flights with Revenue data - ONLY exact "COACH"
        for award_flight in award_flights:
            # Filter: Only include exact "COACH" product type
            if award_flight.get("_product_type") != "COACH":
                continue
            
            segments = award_flight["segments"]
            if not segments:
                continue
            
            dep_time = segments[0]["departure_time"]
            arr_time = segments[-1]["arrival_time"]
            nonstop = award_flight["is_nonstop"]
            key = (dep_time, arr_time, nonstop)
            
            # ONLY include if we found a matching revenue flight with valid price
            if key in revenue_lookup:
                revenue_flight = revenue_lookup[key]
                # Merge: keep award points AND taxes/fees, add revenue cash price
                merged_flight = award_flight.copy()
                merged_flight["cash_price_usd"] = revenue_flight["cash_price_usd"]
                # taxes_fees_usd stays from award_flight
                
                # Recalculate CPP with revenue cash price and award taxes/fees
                if merged_flight["points_required"] > 0:
                    merged_flight["cpp"] = calculate_cpp(
                        revenue_flight["cash_price_usd"],
                        award_flight["taxes_fees_usd"],
                        merged_flight["points_required"]
                    )
                
                # Remove temporary product_type field
                merged_flight.pop("_product_type", None)
                merged_flights.append(merged_flight)
            else:
                # Skip this award flight - no matching revenue data with valid price
                logger.debug(f"Skipping award flight - no matching revenue data: {dep_time} -> {arr_time}")
    
    elif award_flights and not revenue_flights:
        # If only award data, we can't include any flights since we need revenue prices
        logger.warning("⚠️ No revenue data available - cannot calculate CPP. No flights will be saved.")
        logger.warning("   Please ensure both Award and Revenue searches are successful.")
        merged_flights = []
    
    elif revenue_flights and not award_flights:
        # If only revenue data, we don't have points information
        logger.warning("⚠️ No award data available - cannot calculate CPP. No flights will be saved.")
        logger.warning("   Please ensure both Award and Revenue searches are successful.")
        merged_flights = []
    
    else:
        # Neither search succeeded
        logger.error("❌ No flight data available from either search")
        merged_flights = []
    
    # Build the final merged result
    merged_result = {
        "search_metadata": {
            "origin": origin.upper(),
            "destination": destination.upper(),
            "date": date,
            "passengers": 1,
            "cabin_class": CABIN_CLASS_MAP.get(cabin_filter, cabin_filter.lower())
        },
        "flights": merged_flights,
        "total_results": len(merged_flights)
    }
    
    # Save the merged file
    output_file = output_dir / f"{base_filename}_combined.json"
    output_file.write_text(
        json.dumps(merged_result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    
    if merged_flights:
        logger.info(f"✓ Saved merged results: {output_file}")
        logger.info(f"  📈 {len(merged_flights)} flights included (with valid revenue pricing)")
    else:
        logger.warning(f"⚠️ Saved empty results: {output_file}")
        logger.warning(f"  No flights matched between Award and Revenue searches")


# ============================================================================
# CLI
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="American Airlines Flight Scraper - Award & Revenue with Auto Cookie Extraction",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Extract fresh cookies with test search and search actual flights
  %(prog)s --extract-cookies --origin JFK --destination LAX --date 2024-06-15
  
  # Use custom test route for cookie extraction
  %(prog)s --extract-cookies --test-origin SRQ --test-destination BFL --origin JFK --destination LAX --date 2024-06-15
  
  # Extract cookies only (no actual flight search)
  %(prog)s --extract-cookies --cookies-only --test-origin SRQ --test-destination BFL
        """
    )
    
    # Cookie management
    cookie_group = parser.add_argument_group("Cookie Management")
    cookie_group.add_argument(
        "--extract-cookies",
        action="store_true",
        help="Extract fresh cookies using Camoufox with test flight search"
    )
    cookie_group.add_argument(
        "--cookies",
        type=str,
        help=f"Path to cookies JSON file (default: {DEFAULT_COOKIE_FILE})"
    )
    cookie_group.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser in visible mode (only with --extract-cookies)"
    )
    cookie_group.add_argument(
        "--cookies-only",
        action="store_true",
        help="Extract cookies only and exit (no actual flight search)"
    )
    cookie_group.add_argument(
        "--cookie-wait-time",
        type=int,
        default=15,
        help="Seconds to wait during cookie extraction (default: 15)"
    )
    cookie_group.add_argument(
        "--test-origin",
        type=str,
        default="SRQ",
        help="Origin airport for test search during cookie extraction (default: SRQ)"
    )
    cookie_group.add_argument(
        "--test-destination",
        type=str,
        default="BFL",
        help="Destination airport for test search during cookie extraction (default: BFL)"
    )
    cookie_group.add_argument(
        "--test-days-ahead",
        type=int,
        default=7,
        help="Days ahead for test search departure date (default: 7)"
    )
    
    # Flight search parameters
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
        help="Cabin class filter"
    )
    search_group.add_argument(
        "--search-type",
        type=str,
        nargs="+",
        default=["Award", "Revenue"],
        choices=["Award", "Revenue"],
        help="Search types to perform"
    )
    
    # Output and behavior
    config_group = parser.add_argument_group("Configuration")
    config_group.add_argument("--output", type=str, default="./output", help="Output directory")
    config_group.add_argument("--rate-limit", type=float, default=1.0, help="Requests per second")
    config_group.add_argument("--verbose", action="store_true", help="Enable debug logging")
    
    # Logging
    parser.add_argument(
        "--debug-headers",
        action="store_true",
        help="Show detailed header information for debugging"
    )

    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    if args.debug_headers:
        logger.setLevel(logging.DEBUG)
        logger.debug("🔍 Debug mode enabled - showing detailed headers")
    
    # Determine cookie file path
    cookie_file = Path(args.cookies) if args.cookies else DEFAULT_COOKIE_FILE
    
    # Handle cookies-only mode
    if args.cookies_only:
        if not args.extract_cookies:
            logger.error("❌ --cookies-only requires --extract-cookies")
            sys.exit(1)
        
        async def extract_only():
            cookies = await get_cookies(
                extract=True,
                cookie_file=cookie_file,
                headless=not args.no_headless,
                auto_save=True,
                wait_time=args.cookie_wait_time,
                test_origin=args.test_origin,
                test_destination=args.test_destination,
                test_days_ahead=args.test_days_ahead
            )
            if cookies:
                logger.info(f"✓ Cookie extraction complete!")
                logger.info(f"  Saved to: {cookie_file}")
                logger.info(f"  Total cookies: {len(cookies)}")
                logger.info(f"  Test search: {args.test_origin} → {args.test_destination}")
            else:
                logger.error("❌ Cookie extraction failed")
                sys.exit(1)

        
        asyncio.run(extract_only())
        return
    
    # Validate flight search parameters
    if not all([args.origin, args.destination, args.date]):
        logger.error("❌ Missing required flight search parameters")
        logger.error("   Required: --origin, --destination, --date")
        logger.error("   Or use --cookies-only to just extract cookies")
        parser.print_help()
        sys.exit(1)
    
    # Run scraper
    output_dir = Path(args.output)
    
    async def run():
        # Get cookies, headers, and referer
        cookies, headers, referer = await get_cookies(
            extract=args.extract_cookies,
            cookie_file=cookie_file,
            headless=not args.no_headless,
            auto_save=True,
            wait_time=args.cookie_wait_time,
            test_origin=args.test_origin,
            test_destination=args.test_destination,
            test_days_ahead=args.test_days_ahead
        )
        
        if not cookies:
            logger.error("❌ No cookies available - cannot proceed")
            sys.exit(1)
        
        # Search flights with captured headers
        results, raw_responses = await scrape_flights(
            origin=args.origin.upper(),
            destination=args.destination.upper(),
            date=args.date,
            passengers=args.passengers,
            cookies=cookies,
            cabin_filter=args.cabin,
            search_types=args.search_type,
            rate_limit=args.rate_limit,
            captured_headers=headers,
            referer_url=referer
        )
        
        # Check if we got any results
        if not any(results.values()):
            logger.error("❌ All searches failed")
            sys.exit(1)
        
        save_results(
            results,
            raw_responses,
            output_dir,
            args.origin,
            args.destination,
            args.date,
            args.cabin
        )
        
        logger.info(f"\n{'='*60}")
        logger.info(f"✓ Scraping complete!")
        logger.info(f"  Route: {args.origin} → {args.destination}")
        logger.info(f"  Date: {args.date}")
        logger.info(f"  Cabin: {CABIN_CLASS_MAP.get(args.cabin, args.cabin.lower())}")
        for search_type, result in results.items():
            if result:
                logger.info(f"  {search_type}: {len(result)} flights found")
        logger.info(f"  Output: {output_dir}")
        logger.info(f"  Raw data: {output_dir / 'raw_data'}")
        logger.info(f"{'='*60}\n")
    
    asyncio.run(run())


if __name__ == "__main__":
    main()