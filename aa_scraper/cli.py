"""Command-line interface for the AA scraper"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from itertools import product
from .cookie_pool import CookiePool

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
from .date_utils import parse_date_list, validate_date_list, get_date_range_info


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
    
    # Use async context manager for proper cleanup
    async with AAFlightClient(
        cookie_manager, rate_limiter, timeout=DEFAULT_REQUEST_TIMEOUT
    ) as client:
        results = {}
        raw_responses = {}
        
        # Create tasks for concurrent execution
        tasks = []
        for search_type in search_types:
            logger.info(f"Preparing {search_type} search...")
            task = client.search_flights(
                origin, destination, date, passengers, search_type
            )
            tasks.append((search_type, task))
        
        # Execute all searches concurrently
        logger.info(f"Executing {len(tasks)} searches concurrently...")
        responses = await asyncio.gather(*[task for _, task in tasks], return_exceptions=True)
        
        # Process results
        for (search_type, _), api_response in zip(tasks, responses):
            if isinstance(api_response, Exception):
                logger.error(f"{search_type} search failed with exception: {api_response}")
                results[search_type] = None
                raw_responses[search_type] = None
                continue
                
            raw_responses[search_type] = api_response
            
            if not api_response:
                logger.warning(f"{search_type} search returned no data")
                results[search_type] = None
                continue
            
            # Parse flights
            flights = FlightDataParser.parse_flight_options(
                api_response, cabin_filter=cabin_filter, search_type=search_type
            )
            
            if not flights:
                logger.warning(f"No {cabin_filter} flights found in {search_type} response")
                results[search_type] = None
                continue
            
            logger.success(f"Found {len(flights)} {search_type} flights")
            results[search_type] = flights
    
    # Client is automatically closed here
    return results, raw_responses


async def scrape_bulk_concurrent(
    origins: List[str],
    destinations: List[str],
    dates: List[str],
    passengers: int,
    cookie_manager: Optional[CookieManager] = None,  # Single browser (backwards compat)
    cookie_pool: Optional[CookiePool] = None,         # Multi-browser (new)
    cabin_filter: str = "COACH",
    search_types: List[str] = ["Award", "Revenue"],
    rate_limit: float = DEFAULT_RATE_LIMIT,
    max_concurrent: int = 5,
) -> List[Tuple[str, str, str, Dict, Dict]]:
    """
    Scrape multiple origin-destination-date combinations concurrently.
    
    Supports two modes:
    1. Single browser (backwards compatible): Use cookie_manager
    2. Multi-browser (new): Use cookie_pool with round-robin task assignment
    
    Args:
        origins: List of origin airport codes
        destinations: List of destination airport codes
        dates: List of departure dates (YYYY-MM-DD)
        passengers: Number of passengers
        cookie_manager: Single cookie manager (backwards compat)
        cookie_pool: Multi-browser cookie pool (new feature)
        cabin_filter: Cabin class filter
        search_types: List of search types (Award, Revenue)
        rate_limit: Rate limit in requests per second
        max_concurrent: Max concurrent for single browser OR total for multi-browser
    
    Returns:
        List of (origin, destination, date, results, raw_responses) tuples
    """
    # Validate inputs
    if cookie_manager is None and cookie_pool is None:
        raise ValueError("Must provide either cookie_manager or cookie_pool")
    
    if cookie_manager and cookie_pool:
        raise ValueError("Cannot use both cookie_manager and cookie_pool")
    
    # Generate all combinations
    combinations = list(product(origins, destinations, dates))
    total = len(combinations)
    
    # Determine mode
    using_pool = cookie_pool is not None
    
    logger.info("=" * 80)
    if using_pool:
        logger.info(f"🚀 MULTI-BROWSER BULK CONCURRENT SCRAPING")
        logger.info("=" * 80)
        logger.info(f"Browsers:      {cookie_pool.num_browsers}")
        logger.info(f"Per-browser:   {cookie_pool.max_concurrent_per_browser} concurrent")
        logger.info(f"Total max:     {cookie_pool.num_browsers * cookie_pool.max_concurrent_per_browser} concurrent")
    else:
        logger.info(f"🚀 SINGLE-BROWSER BULK CONCURRENT SCRAPING")
        logger.info("=" * 80)
        logger.info(f"Max concurrent: {max_concurrent}")
    
    logger.info(f"Origins:       {', '.join(origins)}")
    logger.info(f"Destinations:  {', '.join(destinations)}")
    
    # Display date information
    total_dates, consecutive_days = get_date_range_info(dates)
    if consecutive_days > 0:
        logger.info(f"Date range:    {dates[0]} to {dates[-1]} ({total_dates} days)")
    else:
        # More than 3 dates, summarize
        if total_dates > 3:
            logger.info(f"Dates:        {dates[0]}, {dates[1]}, {dates[2]} ... {dates[-1]} ({total_dates} total)")
        else:
            logger.info(f"Dates:         {', '.join(dates)}")
    
    logger.info(f"Total combos:  {total}")
    logger.info(f"Search types:  {', '.join(search_types)}")
    logger.info("=" * 80)
    logger.info("")
    
    # Create appropriate semaphore
    if using_pool:
        # No global semaphore - each browser has its own
        semaphore = None
    else:
        # Single browser - use global semaphore
        semaphore = asyncio.Semaphore(max_concurrent)
    
    async def scrape_single_combo(task_id: int, origin: str, dest: str, date: str):
        """Scrape a single origin-destination-date combination"""
        
        # Get cookie manager for this task
        if using_pool:
            browser = cookie_pool.get_browser(task_id)
            task_cookie_manager = browser['manager']
            task_semaphore = browser['semaphore']
            browser_id = browser['id']
        else:
            task_cookie_manager = cookie_manager
            task_semaphore = semaphore
            browser_id = None
        
        # Acquire semaphore
        async with task_semaphore:
            try:
                browser_prefix = f"[Browser #{browser_id}] " if browser_id is not None else ""
                logger.info(f"{browser_prefix}🔍 Starting: {origin} → {dest} on {date}")
                
                results, raw_responses = await scrape_flights(
                    origin=origin,
                    destination=dest,
                    date=date,
                    passengers=passengers,
                    cookie_manager=task_cookie_manager,
                    cabin_filter=cabin_filter,
                    search_types=search_types,
                    rate_limit=rate_limit,
                )
                
                # Count successful results
                success_count = sum(1 for r in results.values() if r is not None)
                total_flights = sum(len(r) for r in results.values() if r is not None)
                
                logger.success(
                    f"{browser_prefix}✅ Completed: {origin} → {dest} on {date} "
                    f"({success_count}/{len(search_types)} searches, {total_flights} flights)"
                )
                
                return (origin, dest, date, results, raw_responses)
                
            except Exception as e:
                browser_prefix = f"[Browser #{browser_id}] " if browser_id is not None else ""
                logger.error(f"{browser_prefix}❌ Failed: {origin} → {dest} on {date}: {e}")
                return (origin, dest, date, None, None)
    
    # Create tasks for all combinations
    tasks = [
        scrape_single_combo(i, origin, dest, date)
        for i, (origin, dest, date) in enumerate(combinations)
    ]
    
    # Execute all tasks concurrently
    if using_pool:
        logger.info(f"⚡ Executing {total} combinations across {cookie_pool.num_browsers} browsers...")
    else:
        logger.info(f"⚡ Executing {total} combinations with max {max_concurrent} concurrent...")
    logger.info("")
    
    start_time = asyncio.get_event_loop().time()
    results = await asyncio.gather(*tasks, return_exceptions=False)
    end_time = asyncio.get_event_loop().time()
    
    duration = end_time - start_time
    
    # Summary
    successful = sum(1 for r in results if r[3] is not None)
    failed = total - successful
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("✅ BULK SCRAPING COMPLETE")
    logger.info("=" * 80)
    logger.info(f"Total combinations: {total}")
    logger.info(f"Successful:        {successful}")
    logger.info(f"Failed:            {failed}")
    logger.info(f"Duration:          {duration:.1f}s")
    logger.info(f"Avg per combo:     {duration/total:.1f}s")
    
    if using_pool:
        logger.info(f"Effective rate:    {successful/duration:.1f} combos/sec")
        logger.info("")
        cookie_pool.print_stats()
    
    logger.info("=" * 80)
    logger.info("")
    
    return results


class DateAction(argparse.Action):
    """Custom action to handle both --date and --dates arguments and store them in the same destination"""
    
    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, **kwargs)
    
    def __call__(self, parser, namespace, values, option_string=None):
        # Initialize dates list if not already present
        if getattr(namespace, self.dest, None) is None:
            setattr(namespace, self.dest, [])
        
        # If value is a list (nargs='+' case), extend
        if isinstance(values, list):
            getattr(namespace, self.dest).extend(values)
        else:
            getattr(namespace, self.dest).append(values)
        

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
    
    # Add both --date and --dates aliases
    date_group = search_group.add_mutually_exclusive_group()
    date_group.add_argument(
        "--date", "--dates",
        dest="dates",
        action=DateAction,
        nargs='+',
        help="Departure date(s) in format YYYY-MM-DD or date range YYYY-MM-DD:YYYY-MM-DD. "
             "Can accept single date, multiple dates, or date ranges."
    )
    
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

    # Bulk search options
    search_group.add_argument(
        "--origins", 
        type=str, 
        nargs="+", 
        help="Multiple origin airport codes for bulk search"
    )
    search_group.add_argument(
        "--destinations", 
        type=str, 
        nargs="+", 
        help="Multiple destination airport codes for bulk search"
    )
    search_group.add_argument(
        "--max-concurrent",
        type=int,
        default=5,
        help="Maximum concurrent requests per browser (default: 5)"
    )
    search_group.add_argument(
        "--browsers",
        type=int,
        default=1,
        help="Number of parallel browsers with different cookies (default: 1). "
             "Total concurrency = browsers × max-concurrent. "
             "Example: --browsers 5 --max-concurrent 5 = 25 total concurrent requests"
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
    logger.info("AA Flight Scraper - Production Ready (v0.2.0)")
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

            # Process dates - can be a single date or multiple dates/ranges
            if not args.dates:
                logger.error("No dates specified. Use --date or --dates to specify date(s) or date range.")
                sys.exit(1)
                
            try:
                dates = parse_date_list(args.dates)
            except ValueError as e:
                logger.error(f"Invalid date specification: {e}")
                sys.exit(1)

            # Check if bulk mode
            origins = args.origins or ([args.origin] if args.origin else None)
            destinations = args.destinations or ([args.destination] if args.destination else None)
            
            is_bulk_mode = bool(len(origins) > 1 if origins else False or 
                              len(destinations) > 1 if destinations else False or 
                              len(dates) > 1)
            
            if is_bulk_mode:
                # Bulk mode validation
                if not all([origins, destinations, dates]):
                    logger.error(
                        "Please specify both origin(s) and destination(s) with date(s) or date range"
                    )
                    sys.exit(1)
                
                # Determine if we need multi-browser mode
                use_multi_browser = args.browsers > 1
                
                if use_multi_browser:
                    logger.info(f"🍪 Multi-browser mode: {args.browsers} browsers")
                    
                    # Create cookie pool
                    cookie_dir = Path("./cookies")
                    cookie_pool = CookiePool(
                        num_browsers=args.browsers,
                        base_cookie_dir=cookie_dir,
                        max_concurrent_per_browser=args.max_concurrent,
                        test_origin=args.test_origin,
                        test_destination=args.test_destination,
                        test_days_ahead=args.test_days_ahead,
                    )
                    
                    # Initialize all browser cookies
                    await cookie_pool.initialize_all_cookies(
                        force_refresh=args.extract_cookies,
                        headless=not args.no_headless,
                        wait_time=args.cookie_wait_time,
                    )
                    
                    # Run bulk scraping with cookie pool
                    bulk_results = await scrape_bulk_concurrent(
                        origins=[o.upper() for o in origins],
                        destinations=[d.upper() for d in destinations],
                        dates=dates,
                        passengers=args.passengers,
                        cookie_pool=cookie_pool,  # Multi-browser mode
                        cabin_filter=args.cabin,
                        search_types=args.search_type,
                        rate_limit=args.rate_limit,
                        max_concurrent=args.max_concurrent,
                    )
                else:
                    logger.info("🍪 Single-browser mode")
                    
                    # Extract cookies if requested
                    if args.extract_cookies:
                        await cookie_manager.get_cookies(
                            force_refresh=True,
                            headless=not args.no_headless,
                            wait_time=args.cookie_wait_time,
                        )
                    
                    # Run bulk scraping with single cookie manager (backwards compat)
                    bulk_results = await scrape_bulk_concurrent(
                        origins=[o.upper() for o in origins],
                        destinations=[d.upper() for d in destinations],
                        dates=dates,
                        passengers=args.passengers,
                        cookie_manager=cookie_manager,  # Single browser mode
                        cabin_filter=args.cabin,
                        search_types=args.search_type,
                        rate_limit=args.rate_limit,
                        max_concurrent=args.max_concurrent,
                    )
                
                # Save all results (same for both modes)
                output_dir = Path(args.output)
                
                successful_count = 0
                for origin, dest, date, results, raw_responses in bulk_results:
                    if results is None:
                        continue
                    
                    try:
                        save_results(
                            results,
                            raw_responses,
                            output_dir,
                            origin,
                            dest,
                            date,
                            args.passengers,
                            args.cabin,
                        )
                        successful_count += 1
                    except Exception as e:
                        logger.error(f"Failed to save {origin}→{dest} on {date}: {e}")
                
                # Final summary
                logger.info("")
                logger.info("=" * 80)
                logger.success(f"✓ Bulk scraping complete! Saved {successful_count} result sets")
                logger.info(f"  Output directory: {output_dir}")
                if use_multi_browser:
                    logger.info(f"  Browsers used: {args.browsers}")
                    logger.info(f"  Per-browser concurrency: {args.max_concurrent}")
                    logger.info(f"  Total effective concurrency: {args.browsers * args.max_concurrent}")
                logger.info("=" * 80)
                
            else:
                # Single-route mode
                if not all([origins and len(origins) == 1, destinations and len(destinations) == 1, dates and len(dates) == 1]):
                    logger.error("Please specify exactly one origin, one destination, and one date.")
                    sys.exit(1)

                # Extract cookies if requested
                if args.extract_cookies:
                    await cookie_manager.get_cookies(
                        force_refresh=True,
                        headless=not args.no_headless,
                        wait_time=args.cookie_wait_time,
                    )

                # Search flights
                origin = origins[0]
                destination = destinations[0]
                date = dates[0]
                logger.info(
                    f"Searching flights: {origin} → {destination} on {date}"
                )

                results, raw_responses = await scrape_flights(
                    origin=origin.upper(),
                    destination=destination.upper(),
                    date=date,
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
                    origin,
                    destination,
                    date,
                    args.passengers,
                    args.cabin,
                )

                # Summary
                logger.info("")
                logger.info("=" * 60)
                logger.success("✓ Scraping complete!")
                logger.info(f"  Route: {origin} → {destination}")
                logger.info(f"  Date: {date}")
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