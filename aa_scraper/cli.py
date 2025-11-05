"""Command-line interface for the AA scraper"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from loguru import logger

from .api_client import AAFlightClient
from .config import (
    CABIN_CLASS_MAP,
    DEFAULT_COOKIE_FILE,
    DEFAULT_RATE_LIMIT,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_TEST_DAYS_AHEAD,
    DEFAULT_TEST_DESTINATION,
    DEFAULT_TEST_ORIGIN,
)
from .cookie_manager import CookieManager
from .logging_config import setup_logging
from .rate_limiter import AdaptiveRateLimiter
from .parser import FlightDataParser
from .storage import save_results


async def scrape_flights(
    origin: str,
    destination: str,
    date: str,
    passengers: int,
    cookie_manager: CookieManager,
    cabin_filter: str = "COACH",
    search_types: List[str] = ["Award", "Revenue"],
    rate_limit: float = DEFAULT_RATE_LIMIT,
) -> Tuple[Dict[str, Optional[List[Dict[str, Any]]]], Dict[str, Optional[Dict[str, Any]]]]:
    """
    Main scraping function with enhanced error handling.

    Args:
        origin: Origin airport code
        destination: Destination airport code
        date: Departure date (YYYY-MM-DD)
        passengers: Number of passengers
        cookie_manager: Cookie manager instance
        cabin_filter: Cabin class filter
        search_types: List of search types (Award, Revenue)
        rate_limit: Rate limit in requests per second

    Returns:
        Tuple of (results, raw_responses)
    """
    rate_limiter = AdaptiveRateLimiter(rate=rate_limit, burst=int(rate_limit * 2))
    client = AAFlightClient(cookie_manager, rate_limiter, timeout=DEFAULT_REQUEST_TIMEOUT)

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
            api_response, cabin_filter=cabin_filter, search_type=search_type
        )

        if not flights:
            logger.warning(f"⚠️ No {cabin_filter} flights found in {search_type} response")
            results[search_type] = None
            continue

        logger.success(f"✓ Found {len(flights)} {search_type} flights")
        results[search_type] = flights

    return results, raw_responses


def main() -> None:
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        description="American Airlines Flight Scraper - Production Ready",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Cookie management
    cookie_group = parser.add_argument_group("Cookie Management")
    cookie_group.add_argument(
        "--extract-cookies", action="store_true", help="Extract fresh cookies"
    )
    cookie_group.add_argument("--cookies", type=str, help="Cookie file path")
    cookie_group.add_argument(
        "--no-headless", action="store_true", help="Visible browser mode"
    )
    cookie_group.add_argument(
        "--cookies-only", action="store_true", help="Extract cookies only, no search"
    )
    cookie_group.add_argument(
        "--cookie-wait-time", type=int, default=15, help="Cookie extraction wait time"
    )
    cookie_group.add_argument(
        "--test-origin", type=str, default=DEFAULT_TEST_ORIGIN, help="Test origin for cookies"
    )
    cookie_group.add_argument(
        "--test-destination",
        type=str,
        default=DEFAULT_TEST_DESTINATION,
        help="Test destination for cookies",
    )
    cookie_group.add_argument(
        "--test-days-ahead",
        type=int,
        default=DEFAULT_TEST_DAYS_AHEAD,
        help="Test date offset",
    )

    # Flight search
    search_group = parser.add_argument_group("Flight Search")
    search_group.add_argument("--origin", type=str, help="Origin airport code")
    search_group.add_argument("--destination", type=str, help="Destination airport code")
    search_group.add_argument("--date", type=str, help="Departure date (YYYY-MM-DD)")
    search_group.add_argument(
        "--passengers", type=int, default=1, help="Number of passengers"
    )
    search_group.add_argument(
        "--cabin",
        type=str,
        default="COACH",
        choices=["COACH", "BUSINESS", "FIRST", "PREMIUM_ECONOMY"],
        help="Cabin class",
    )
    search_group.add_argument(
        "--search-type",
        type=str,
        nargs="+",
        default=["Award", "Revenue"],
        choices=["Award", "Revenue"],
        help="Search types",
    )

    # Configuration
    config_group = parser.add_argument_group("Configuration")
    config_group.add_argument(
        "--output", type=str, default="./output", help="Output directory"
    )
    config_group.add_argument(
        "--rate-limit", type=float, default=DEFAULT_RATE_LIMIT, help="Requests per second"
    )
    config_group.add_argument("--verbose", action="store_true", help="Debug logging")
    config_group.add_argument("--log-file", type=str, help="Log file path")

    args = parser.parse_args()

    # Setup logging
    log_file = Path(args.log_file) if args.log_file else Path("./logs/aa_scraper.log")
    setup_logging(verbose=args.verbose, log_file=log_file)

    logger.info("=" * 60)
    logger.info("AA Flight Scraper - Production Ready")
    logger.info("=" * 60)

    # Cookie file path
    cookie_file = Path(args.cookies) if args.cookies else DEFAULT_COOKIE_FILE

    # Initialize cookie manager
    cookie_manager = CookieManager(
        cookie_file=cookie_file,
        test_origin=args.test_origin,
        test_destination=args.test_destination,
        test_days_ahead=args.test_days_ahead,
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
                    wait_time=args.cookie_wait_time,
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
                    wait_time=args.cookie_wait_time,
                )

            # Search flights
            logger.info(
                f"Searching flights: {args.origin} → {args.destination} on {args.date}"
            )

            results, raw_responses = await scrape_flights(
                origin=args.origin.upper(),
                destination=args.destination.upper(),
                date=args.date,
                passengers=args.passengers,
                cookie_manager=cookie_manager,
                cabin_filter=args.cabin,
                search_types=args.search_type,
                rate_limit=args.rate_limit,
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
                args.cabin,
            )

            # Summary
            logger.info("")
            logger.info("=" * 60)
            logger.success("✓ Scraping complete!")
            logger.info(f"  Route: {args.origin} → {args.destination}")
            logger.info(f"  Date: {args.date}")
            logger.info(f"  Cabin: {CABIN_CLASS_MAP.get(args.cabin, args.cabin.lower())}")

            for search_type, result in results.items():
                if result:
                    logger.info(f"  {search_type}: {len(result)} flights")

            logger.info(f"  Output: {output_dir}")
            logger.info("=" * 60)

        except KeyboardInterrupt:
            logger.warning("Interrupted by user")
            sys.exit(1)
        except Exception as e:
            logger.exception(f"Fatal error: {e}")
            sys.exit(1)

    asyncio.run(run())


if __name__ == "__main__":
    main()