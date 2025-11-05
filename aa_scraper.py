#!/usr/bin/env python3
"""
American Airlines Flight Scraper - Operation Point Break
Production-ready async scraper with bot evasion and data storage
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import httpx
from tqdm.asyncio import tqdm


# ============================================================================
# Configuration
# ============================================================================

API_ENDPOINT = "https://www.aa.com/booking/api/search/itinerary"
BASE_URL = "https://www.aa.com"

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
    segments: List[FlightSegment]
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
    cabin_class: str


@dataclass
class FlightSearchResult:
    """Complete search result"""
    search_metadata: SearchMetadata
    flights: List[FlightOption]
    total_results: int


# ============================================================================
# Helper Functions
# ============================================================================

def utc_timestamp() -> str:
    """Get current UTC timestamp"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_directory(path: Path):
    """Create directory if it doesn't exist"""
    path.mkdir(parents=True, exist_ok=True)


def load_cookies_from_file(cookie_file: Path) -> Dict[str, str]:
    """
    Load cookies from a file.
    
    Supported formats:
    1. JSON: {"XSRF-TOKEN": "value", "JSESSIONID": "value"}
    2. Netscape/curl format: name\tvalue (tab-separated)
    3. Simple key=value pairs (one per line)
    """
    cookies = {}
    
    if not cookie_file.exists():
        logger.error(f"Cookie file not found: {cookie_file}")
        return cookies
    
    content = cookie_file.read_text(encoding="utf-8").strip()
    
    # Try JSON format first
    try:
        cookies = json.loads(content)
        logger.info(f"Loaded {len(cookies)} cookies from JSON file")
        return cookies
    except json.JSONDecodeError:
        pass
    
    # Try line-by-line formats
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        
        # Try tab-separated (Netscape format)
        if "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
        # Try key=value format
        elif "=" in line:
            key, value = line.split("=", 1)
            cookies[key.strip()] = value.strip()
    
    logger.info(f"Loaded {len(cookies)} cookies from file")
    return cookies


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
        timeout: float = 30.0
    ):
        self.cookies = cookies
        self.rate_limiter = rate_limiter
        self.timeout = timeout
        
        # Extract critical headers from cookies if available
        self.headers = HEADERS.copy()
        if "XSRF-TOKEN" in cookies:
            self.headers["X-XSRF-TOKEN"] = cookies["XSRF-TOKEN"]

        if "spa_session_id" in cookies:
            self.headers["X-CID"] = cookies["spa_session_id"]
        
        # Use a valid referrer (from the browser when you captured cookies)
        self.headers["Referer"] = "https://www.aa.com/booking/choose-flights/1"
    
    def _build_request_payload(
        self,
        origin: str,
        destination: str,
        date: str,
        passengers: int = 1
    ) -> Dict[str, Any]:
        """Build the API request payload"""
        return {
            "metadata": {
                "selectedProducts": [],
                "tripType": "OneWay",
                "udo": {}  # ← Added
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
                    "cabin": "",  # ← Changed from {} to ""
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
                "pointOfSale": "",  # ← Added
                "searchType": "Award"
            },
            "loyaltyInfo": None,  # ← Added
            "version": "",  # ← Added
            "queryParams": {  # ← Added all missing fields
                "sliceIndex": 0,
                "sessionId": "",
                "solutionSet": "",
                "solutionId": "",
                "sort": "CARRIER"
            }
        }

    
    async def search_flights(
        self,
        origin: str,
        destination: str,
        date: str,
        passengers: int = 1
    ) -> Optional[Dict[str, Any]]:
        """Search for flights"""
        await self.rate_limiter.acquire()
        
        payload = self._build_request_payload(origin, destination, date, passengers)
        
        limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
        timeout = httpx.Timeout(self.timeout)
        
        async with httpx.AsyncClient(
            headers=self.headers,
            cookies=self.cookies,
            timeout=timeout,
            limits=limits,
            http2=True,
            verify=True,
            follow_redirects=False
        ) as client:
            try:
                logger.info(f"Searching flights: {origin} → {destination} on {date}")
                response = await client.post(API_ENDPOINT, json=payload)
                response.raise_for_status()
                
                data = response.json()
                logger.info(f"✓ Search successful - {len(data.get('slices', []))} flights found")
                return data
                
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    logger.error("❌ 403 Forbidden - Check your cookies (likely expired/invalid)")
                elif e.response.status_code == 429:
                    logger.warning("⚠️ Rate limited - Consider reducing request rate")
                else:
                    logger.error(f"HTTP {e.response.status_code}: {e}")
                return None
            except Exception as e:
                logger.error(f"Request failed: {e}")
                return None


# ============================================================================
# Data Parser
# ============================================================================

class FlightDataParser:
    """Parse AA API response into structured data"""
    
    @staticmethod
    def parse_segments(segments_data: List[Dict]) -> List[FlightSegment]:
        """Parse flight segments"""
        segments = []
        for seg in segments_data:
            flight = seg.get("flight", {})
            flight_num = f"{flight.get('carrierCode', '')}{flight.get('flightNumber', '')}"
            
            # Get first leg times (segments contain legs)
            legs = seg.get("legs", [])
            if legs:
                leg = legs[0]
                departure = leg.get("departureDateTime", "")
                arrival = leg.get("arrivalDateTime", "")
                
                # Extract just the time (HH:MM)
                dep_time = departure.split("T")[1][:5] if "T" in departure else ""
                arr_time = arrival.split("T")[1][:5] if "T" in arrival else ""
                
                segments.append(FlightSegment(
                    flight_number=flight_num,
                    departure_time=dep_time,
                    arrival_time=arr_time
                ))
        
        return segments
    
    @staticmethod
    def parse_flight_options(
        api_response: Dict[str, Any],
        cabin_class: str = "COACH"
    ) -> List[FlightOption]:
        """
        Parse flight options from API response.
        Filters for specified cabin class.
        """
        flights = []
        slices = api_response.get("slices", [])
        
        for slice_data in slices:
            # Extract flight metadata
            duration_min = slice_data.get("durationInMinutes", 0)
            duration_str = format_duration(duration_min)
            is_nonstop = slice_data.get("stops", 0) == 0
            
            # Get departure/arrival times
            departure_dt = slice_data.get("departureDateTime", "")
            arrival_dt = slice_data.get("arrivalDateTime", "")
            
            dep_time = departure_dt.split("T")[1][:5] if "T" in departure_dt else ""
            arr_time = arrival_dt.split("T")[1][:5] if "T" in arrival_dt else ""
            
            origin = slice_data.get("origin", {}).get("code", "")
            destination = slice_data.get("destination", {}).get("code", "")
            
            # Iterate through product pricing options (COACH, BUSINESS, FIRST, etc.)
            product_pricing = slice_data.get("productPricing", [])
            
            for pricing_option in product_pricing:
                # Use cheapestPrice if available, otherwise regularPrice
                price_info = pricing_option.get("cheapestPrice") or pricing_option.get("regularPrice")
                
                if not price_info:
                    continue
                
                # Check if this matches requested cabin class
                product_type = price_info.get("productType", "")
                if product_type != cabin_class:
                    continue
                
                # Check if product is available
                if not price_info.get("productAvailable", False):
                    continue
                
                # Extract pricing information from slicePricing
                slice_pricing = price_info.get("slicePricing", {})
                
                if not slice_pricing:
                    continue
                
                # Points required
                points_str = slice_pricing.get("perPassengerAwardPoints", "0")
                if isinstance(points_str, str):
                    points = int(float(points_str.replace(",", "")))
                else:
                    points = int(points_str)
                
                # Taxes and fees
                taxes_fees = slice_pricing.get("allPassengerDisplayTaxTotal", {}).get("amount", 0.0)
                
                # Total price
                cash_total = slice_pricing.get("allPassengerDisplayTotal", {}).get("amount", 0.0)
                
                # Calculate CPP
                cpp = calculate_cpp(cash_total, taxes_fees, points)
                
                # Create flight segment
                segments = [FlightSegment(
                    flight_number=f"{origin}{destination}",
                    departure_time=dep_time,
                    arrival_time=arr_time
                )]
                
                flight = FlightOption(
                    is_nonstop=is_nonstop,
                    segments=[asdict(seg) for seg in segments],
                    total_duration=duration_str,
                    points_required=points,
                    cash_price_usd=cash_total,
                    taxes_fees_usd=taxes_fees,
                    cpp=cpp
                )
                
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
    rate_limit: float = 1.0
) -> Optional[FlightSearchResult]:
    """
    Main scraping function
    
    Args:
        origin: Origin airport code (e.g., 'LAX')
        destination: Destination airport code (e.g., 'JFK')
        date: Departure date in YYYY-MM-DD format
        passengers: Number of passengers
        cookies: Cookie dictionary
        rate_limit: Requests per second
    
    Returns:
        FlightSearchResult or None
    """
    rate_limiter = RateLimiter(rate=rate_limit, burst=int(rate_limit * 2))
    client = AAFlightClient(cookies, rate_limiter)
    
    # Search flights
    api_response = await client.search_flights(origin, destination, date, passengers)
    
    if not api_response:
        return None
    
    # Parse response
    flights = FlightDataParser.parse_flight_options(api_response)
    
    if not flights:
        logger.warning("No Main Cabin flights found in response")
        return None
    
    # Build result
    metadata = SearchMetadata(
        origin=origin,
        destination=destination,
        date=date,
        passengers=passengers,
        cabin_class="economy"
    )
    
    result = FlightSearchResult(
        search_metadata=asdict(metadata),
        flights=[asdict(f) for f in flights],
        total_results=len(flights)
    )
    
    return result


# ============================================================================
# Storage
# ============================================================================

def save_results(
    result: FlightSearchResult,
    output_dir: Path,
    format: str = "json"
):
    """
    Save scraping results
    
    Args:
        result: FlightSearchResult to save
        output_dir: Output directory
        format: 'json' or 'jsonl'
    """
    ensure_directory(output_dir)
    
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    metadata = result.search_metadata
    filename = f"{metadata['origin']}_{metadata['destination']}_{metadata['date']}_{timestamp}"
    
    result_dict = asdict(result)
    
    if format == "json":
        output_file = output_dir / f"{filename}.json"
        output_file.write_text(
            json.dumps(result_dict, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        logger.info(f"✓ Saved JSON: {output_file}")
    
    elif format == "jsonl":
        output_file = output_dir / f"{filename}.jsonl"
        with output_file.open("w", encoding="utf-8") as f:
            for flight in result.flights:
                f.write(json.dumps(flight, ensure_ascii=False) + "\n")
        logger.info(f"✓ Saved JSONL: {output_file}")
    
    # Also save metadata separately
    meta_file = output_dir / f"{filename}_metadata.json"
    meta_file.write_text(
        json.dumps({
            "search_metadata": result.search_metadata,
            "total_results": result.total_results,
            "scraped_at": utc_timestamp()
        }, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="American Airlines Flight Scraper - Operation Point Break",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic search with cookie file
  python aa_scraper.py --origin LAX --destination JFK --date 2025-12-15 --cookies cookies.json
  
  # With custom output directory
  python aa_scraper.py --origin LAX --destination JFK --date 2025-12-15 --cookies cookies.json --output ./data
  
  # Lower rate limit for safety
  python aa_scraper.py --origin LAX --destination JFK --date 2025-12-15 --cookies cookies.json --rate-limit 0.5

Cookie File Formats:
  1. JSON: {"XSRF-TOKEN": "abc", "JSESSIONID": "xyz", "_abck": "..."}
  2. Key=Value: One cookie per line (XSRF-TOKEN=abc)
  3. Netscape/curl format: Tab-separated values
        """
    )
    
    # Required arguments
    parser.add_argument(
        "--origin",
        type=str,
        required=True,
        help="Origin airport code (e.g., LAX)"
    )
    parser.add_argument(
        "--destination",
        type=str,
        required=True,
        help="Destination airport code (e.g., JFK)"
    )
    parser.add_argument(
        "--date",
        type=str,
        required=True,
        help="Departure date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--cookies",
        type=str,
        required=True,
        help="Path to cookies file (JSON or text format)"
    )
    
    # Optional arguments
    parser.add_argument(
        "--passengers",
        type=int,
        default=1,
        help="Number of passengers (default: 1)"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="./output",
        help="Output directory (default: ./output)"
    )
    parser.add_argument(
        "--format",
        type=str,
        choices=["json", "jsonl"],
        default="json",
        help="Output format (default: json)"
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=1.0,
        help="Requests per second (default: 1.0)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging"
    )
    
    args = parser.parse_args()
    
    # Configure logging
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Load cookies
    cookie_path = Path(args.cookies)
    cookies = load_cookies_from_file(cookie_path)
    
    if not cookies:
        logger.error("❌ No cookies loaded - cannot proceed")
        logger.info("\nTo get cookies:")
        logger.info("1. Visit https://www.aa.com in your browser")
        logger.info("2. Open DevTools (F12) → Application → Cookies")
        logger.info("3. Export cookies to JSON format")
        logger.info("4. Save critical cookies: XSRF-TOKEN, JSESSIONID, _abck, bm_sv, etc.")
        sys.exit(1)
    
    # Validate required cookies
    required_cookies = ["XSRF-TOKEN", "JSESSIONID"]
    missing = [c for c in required_cookies if c not in cookies]
    if missing:
        logger.warning(f"⚠️ Missing recommended cookies: {', '.join(missing)}")
        logger.warning("Scraper may fail without these cookies")
    
    # Run scraper
    output_dir = Path(args.output)
    
    async def run():
        result = await scrape_flights(
            origin=args.origin.upper(),
            destination=args.destination.upper(),
            date=args.date,
            passengers=args.passengers,
            cookies=cookies,
            rate_limit=args.rate_limit
        )
        
        if result:
            save_results(result, output_dir, args.format)
            logger.info(f"\n{'='*60}")
            logger.info(f"✓ Scraping complete!")
            logger.info(f"  Route: {args.origin} → {args.destination}")
            logger.info(f"  Date: {args.date}")
            logger.info(f"  Flights found: {result.total_results}")
            logger.info(f"  Output: {output_dir}")
            logger.info(f"{'='*60}\n")
        else:
            logger.error("❌ Scraping failed")
            sys.exit(1)
    
    asyncio.run(run())


if __name__ == "__main__":
    main()
