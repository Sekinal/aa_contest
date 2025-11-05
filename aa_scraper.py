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
    cabin_class: str
    product_type: str


@dataclass
class SearchMetadata:
    """Search parameters"""
    origin: str
    destination: str
    date: str
    passengers: int
    search_type: str  # "Award" or "Revenue"


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
            
            # Iterate through product pricing options
            pricing_detail = slice_data.get("pricingDetail", [])
            
            for pricing_option in pricing_detail:
                # Check if product is available
                if not pricing_option.get("productAvailable", False):
                    continue
                
                # Get product type
                product_type = pricing_option.get("productType", "")
                product_group = pricing_option.get("productGroup", "")
                
                # Filter by cabin class (match on product_type)
                if cabin_filter:
                    # Match COACH, BUSINESS, FIRST, etc.
                    if not product_type.startswith(cabin_filter):
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
                    fare_amount = 0.0
                else:
                    points = 0
                    fare_amount = points_or_fare
                
                # Taxes and fees
                taxes_fees = slice_pricing.get("allPassengerDisplayTaxTotal", {}).get("amount", 0.0)
                
                # Total price
                cash_total = slice_pricing.get("allPassengerDisplayTotal", {}).get("amount", 0.0)
                
                # Calculate CPP (only meaningful for Award)
                if search_type == "Award":
                    cpp = calculate_cpp(cash_total, taxes_fees, points)
                else:
                    cpp = 0.0
                
                # Create flight segment
                segments = [FlightSegment(
                    flight_number=f"{origin}-{destination}",
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
                    cpp=cpp,
                    cabin_class=product_group,
                    product_type=product_type
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
    cabin_filter: str = "COACH",
    search_types: List[str] = ["Award", "Revenue"],
    rate_limit: float = 1.0
) -> tuple[Dict[str, Optional[FlightSearchResult]], Dict[str, Optional[Dict[str, Any]]]]:
    """
    Main scraping function - scrapes both Award and Revenue searches
    
    Returns:
        Tuple of:
        - Dictionary with keys "Award" and "Revenue" containing parsed results
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
        
        # Build result
        metadata = SearchMetadata(
            origin=origin,
            destination=destination,
            date=date,
            passengers=passengers,
            search_type=search_type
        )
        
        result = FlightSearchResult(
            search_metadata=asdict(metadata),
            flights=[asdict(f) for f in flights],
            total_results=len(flights)
        )
        
        results[search_type] = result
    
    return results, raw_responses


# ============================================================================
# Storage
# ============================================================================


def save_results(
    results: Dict[str, Optional[FlightSearchResult]],
    raw_responses: Dict[str, Optional[Dict[str, Any]]],
    output_dir: Path,
    origin: str,
    destination: str,
    date: str
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
    # Process and save merged results (existing logic)
    # ========================================================================
    
    # Convert FlightSearchResult objects to dictionaries
    award_result = asdict(results["Award"]) if results.get("Award") else None
    revenue_result = asdict(results["Revenue"]) if results.get("Revenue") else None
    
    # Create a merged flight list
    merged_flights = []
    
    if award_result and revenue_result:
        # Create a lookup dict for revenue flights - ONLY exact "COACH" product type
        revenue_lookup = {}
        for flight in revenue_result["flights"]:
            # Filter: Only include exact "COACH" product type
            if flight["product_type"] != "COACH":
                continue
                
            dep_time = flight["segments"][0]["departure_time"]
            arr_time = flight["segments"][0]["arrival_time"]
            nonstop = flight["is_nonstop"]
            key = (dep_time, arr_time, nonstop)
            
            if key not in revenue_lookup:
                revenue_lookup[key] = flight
        
        # Merge Award flights with Revenue data - ONLY exact "COACH"
        for award_flight in award_result["flights"]:
            # Filter: Only include exact "COACH" product type
            if award_flight["product_type"] != "COACH":
                continue
            
            dep_time = award_flight["segments"][0]["departure_time"]
            arr_time = award_flight["segments"][0]["arrival_time"]
            nonstop = award_flight["is_nonstop"]
            key = (dep_time, arr_time, nonstop)
            
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
            else:
                merged_flight = award_flight.copy()
            
            merged_flights.append(merged_flight)
    
    elif award_result:
        # Filter for exact "COACH" only
        merged_flights = [f for f in award_result["flights"] if f["product_type"] == "COACH"]
    elif revenue_result:
        # Filter for exact "COACH" only
        merged_flights = [f for f in revenue_result["flights"] if f["product_type"] == "COACH"]
    
    # Build the final merged result
    merged_result = {
        "search_metadata": {
            "origin": origin.upper(),
            "destination": destination.upper(),
            "date": date,
            "passengers": 1,
            "search_types": [k for k, v in results.items() if v is not None]
        },
        "flights": merged_flights,
        "total_results": len(merged_flights)
    }
    
    # Save ONLY the single merged file
    output_file = output_dir / f"{base_filename}_combined.json"
    output_file.write_text(
        json.dumps(merged_result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    logger.info(f"✓ Saved merged results: {output_file}")


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
        
        save_results(results, raw_responses, output_dir, args.origin, args.destination, args.date)
        
        logger.info(f"\n{'='*60}")
        logger.info(f"✓ Scraping complete!")
        logger.info(f"  Route: {args.origin} → {args.destination}")
        logger.info(f"  Date: {args.date}")
        for search_type, result in results.items():
            if result:
                logger.info(f"  {search_type}: {result.total_results} flights found")
        logger.info(f"  Output: {output_dir}")
        logger.info(f"  Raw data: {output_dir / 'raw_data'}")
        logger.info(f"{'='*60}\n")
    
    asyncio.run(run())


if __name__ == "__main__":
    main()