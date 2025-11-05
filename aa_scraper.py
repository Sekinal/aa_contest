#!/usr/bin/env python3
"""
American Airlines Flight Scraper - Operation Point Break
Production-ready async scraper with bot evasion and data storage
Supports both Award (points) and Revenue (cash) searches
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


def load_cookies_from_file(cookie_file: Path) -> Dict[str, str]:
    """Load cookies from a file (JSON format)"""
    cookies = {}
    
    if not cookie_file.exists():
        logger.error(f"Cookie file not found: {cookie_file}")
        return cookies
    
    content = cookie_file.read_text(encoding="utf-8").strip()
    
    try:
        cookies = json.loads(content)
        logger.info(f"Loaded {len(cookies)} cookies from JSON file")
        return cookies
    except json.JSONDecodeError:
        logger.error("Failed to parse cookie file as JSON")
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
        
        # Use a valid referrer
        self.headers["Referer"] = "https://www.aa.com/booking/choose-flights/1"
    
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
        passengers: int = 1,
        search_type: str = "Award"
    ) -> Optional[Dict[str, Any]]:
        """Search for flights"""
        await self.rate_limiter.acquire()
        
        payload = self._build_request_payload(origin, destination, date, passengers, search_type)
        
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
                logger.info(f"Searching {search_type} flights: {origin} → {destination} on {date}")
                response = await client.post(API_ENDPOINT, json=payload)
                response.raise_for_status()
                
                data = response.json()
                logger.info(f"✓ {search_type} search successful - {len(data.get('slices', []))} flights found")
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
    rate_limit: float = 1.0
) -> tuple[Dict[str, Optional[List[Dict[str, Any]]]], Dict[str, Optional[Dict[str, Any]]]]:
    """
    Main scraping function - scrapes both Award and Revenue searches
    
    Returns:
        Tuple of:
        - Dictionary with keys "Award" and "Revenue" containing parsed flight lists
        - Dictionary with keys "Award" and "Revenue" containing raw API responses
    """
    rate_limiter = RateLimiter(rate=rate_limit, burst=int(rate_limit * 2))
    client = AAFlightClient(cookies, rate_limiter)
    
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
            logger.warning(f"No {cabin_filter} flights found in {search_type} response")
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
        
        logger.info(f"Found {len(revenue_lookup)} valid revenue flights with pricing")
        
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
        logger.info(f"  {len(merged_flights)} flights included (with valid revenue pricing)")
    else:
        logger.warning(f"⚠️ Saved empty results: {output_file}")
        logger.warning(f"  No flights matched between Award and Revenue searches")


# ============================================================================
# CLI
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="American Airlines Flight Scraper - Award & Revenue",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # Required arguments
    parser.add_argument("--origin", type=str, required=True, help="Origin airport code")
    parser.add_argument("--destination", type=str, required=True, help="Destination airport code")
    parser.add_argument("--date", type=str, required=True, help="Departure date (YYYY-MM-DD)")
    parser.add_argument("--cookies", type=str, required=True, help="Path to cookies JSON file")
    
    # Optional arguments
    parser.add_argument("--passengers", type=int, default=1, help="Number of passengers")
    parser.add_argument("--cabin", type=str, default="COACH", 
                       choices=["COACH", "BUSINESS", "FIRST", "PREMIUM_ECONOMY"],
                       help="Cabin class filter")
    parser.add_argument("--search-type", type=str, nargs="+", 
                       default=["Award", "Revenue"],
                       choices=["Award", "Revenue"],
                       help="Search types to perform")
    parser.add_argument("--output", type=str, default="./output", help="Output directory")
    parser.add_argument("--rate-limit", type=float, default=1.0, help="Requests per second")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Load cookies
    cookie_path = Path(args.cookies)
    cookies = load_cookies_from_file(cookie_path)
    
    if not cookies:
        logger.error("❌ No cookies loaded - cannot proceed")
        sys.exit(1)
    
    # Run scraper
    output_dir = Path(args.output)
    
    async def run():
        results, raw_responses = await scrape_flights(
            origin=args.origin.upper(),
            destination=args.destination.upper(),
            date=args.date,
            passengers=args.passengers,
            cookies=cookies,
            cabin_filter=args.cabin,
            search_types=args.search_type,
            rate_limit=args.rate_limit
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