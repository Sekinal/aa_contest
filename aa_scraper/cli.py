"""Command-line interface for the AA scraper with async streaming storage"""

import argparse
import asyncio
import gc
import sys
from datetime import datetime, timezone
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
from .exceptions import (
    AAScraperError,
    CircuitOpenError,
    CookieExpiredError,
    RateLimitError,
    IPBlockedError,
)
from .proxy_pool import ProxyPool
from .cookie_manager import CookieManager
from .logging_config import setup_logging
from .rate_limiter import AdaptiveRateLimiter
from .parser import FlightDataParser
from .storage import save_results_streaming
from .date_utils import parse_date_list, validate_date_list, get_date_range_info

_FILE_IO_SEMAPHORE = None
# Global rate limiter shared across all combos
_SHARED_RATE_LIMITER = None

def _get_shared_rate_limiter(rate: float, burst: int):
    """Get or create the global shared rate limiter"""
    global _SHARED_RATE_LIMITER
    if _SHARED_RATE_LIMITER is None:
        _SHARED_RATE_LIMITER = AdaptiveRateLimiter(rate=rate, burst=burst)
    return _SHARED_RATE_LIMITER

def _get_file_io_semaphore(max_concurrent_writes: int = 3):
    """Get or create the global file I/O semaphore"""
    global _FILE_IO_SEMAPHORE
    if _FILE_IO_SEMAPHORE is None:
        _FILE_IO_SEMAPHORE = asyncio.Semaphore(max_concurrent_writes)
    return _FILE_IO_SEMAPHORE

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
            try:
                flights = FlightDataParser.parse_flight_options(
                    api_response, cabin_filter=cabin_filter, search_type=search_type
                )
            except Exception as e:
                logger.warning(f"Failed to parse {search_type} flights: {e}")
                results[search_type] = None
                continue
            
            if not flights:
                logger.warning(f"No {cabin_filter} flights found in {search_type} response")
                results[search_type] = None
                continue
            
            logger.success(f"Found {len(flights)} {search_type} flights")
            results[search_type] = flights
    
    # Client is automatically closed here
    return results, raw_responses


async def scrape_flights_with_metrics(
    origin: str,
    destination: str,
    date: str,
    passengers: int,
    cookie_manager: CookieManager,
    cabin_filter: str = "COACH",
    search_types: List[str] = ["Award", "Revenue"],
    rate_limiter: Optional[AdaptiveRateLimiter] = None,
    rate_limit: float = DEFAULT_RATE_LIMIT,
) -> Tuple[Dict, Dict, Dict]:
    """
    Scrape flights with metrics tracking - CONCURRENT execution of Award/Revenue.
    
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
        Tuple of (results, raw_responses, metrics_dict)
    """
    rate_limiter = AdaptiveRateLimiter(rate=rate_limit, burst=int(rate_limit * 2))
    
    # Initialize metrics tracking
    metrics = {
        'api_requests': 0,
        'responses_bytes': 0,
        'retries': 0,
        'response_times': [],
        'cookie_refreshes': 0,
    }

    async with AAFlightClient(cookie_manager, rate_limiter, timeout=DEFAULT_REQUEST_TIMEOUT) as client:
        results = {}
        raw_responses = {}
        
        # 🔥 CREATE CONCURRENT TASKS (instead of sequential loop)
        tasks = []
        for search_type in search_types:
            task = client.search_flights(
                origin, destination, date, passengers, search_type
            )
            tasks.append((search_type, task))
        
        # 🚀 EXECUTE ALL SEARCHES CONCURRENTLY
        import time
        start_time = time.time()
        
        responses = await asyncio.gather(
            *[task for _, task in tasks], 
            return_exceptions=True
        )
        
        elapsed = time.time() - start_time
        
        # Process results
        for (search_type, _), api_response in zip(tasks, responses):
            metrics['api_requests'] += 1
            
            # Track response time (divided by number of concurrent requests for average)
            metrics['response_times'].append(elapsed / len(search_types))
            
            if isinstance(api_response, Exception):
                logger.error(f"{search_type} search failed with exception: {api_response}")
                results[search_type] = None
                raw_responses[search_type] = None
                continue
            
            raw_responses[search_type] = api_response
            
            # Count response bytes
            if api_response:
                import json
                response_json = json.dumps(api_response)
                metrics['responses_bytes'] += len(response_json.encode())
            
            if not api_response:
                logger.warning(f"{search_type} search returned no data")
                results[search_type] = None
                continue
            
            # Parse flights
            try:
                flights = FlightDataParser.parse_flight_options(
                    api_response, cabin_filter=cabin_filter, search_type=search_type
                )
            except Exception as e:
                logger.warning(f"Failed to parse {search_type} flights: {e}")
                results[search_type] = None
                continue
            
            if not flights:
                logger.warning(f"No {cabin_filter} flights found in {search_type} response")
                results[search_type] = None
                continue
            
            logger.success(f"Found {len(flights)} {search_type} flights")
            results[search_type] = flights
    
    return results, raw_responses, metrics


async def scrape_bulk_concurrent(
    origins: List[str],
    destinations: List[str],
    dates: List[str],
    passengers: int,
    cookie_manager: Optional[CookieManager] = None,
    cookie_pool: Optional[CookiePool] = None,
    cabin_filter: str = "COACH",
    search_types: List[str] = ["Award", "Revenue"],
    rate_limit: float = DEFAULT_RATE_LIMIT,
    max_concurrent: int = 5,
    output_dir: Path = Path("./output"),
) -> Dict[str, Any]:
    """
    Memory-efficient bulk scraping with streaming storage.
    Includes comprehensive metrics tracking.
    
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
        output_dir: Output directory for results
    
    Returns:
        Summary statistics dict
    """
    # Validate inputs
    if cookie_manager is None and cookie_pool is None:
        raise ValueError("Must provide either cookie_manager or cookie_pool")
    
    if cookie_manager and cookie_pool:
        raise ValueError("Cannot use both cookie_manager and cookie_pool")
    
    # Generate all combinations
    combinations = list(product(origins, destinations, dates))
    total = len(combinations)
    
    # ✅ CREATE STORAGE ONCE (pre-create directories)
    from .storage import AsyncStreamingStorage
    global_storage = AsyncStreamingStorage(output_dir)
    logger.info(f"💾 Pre-initialized storage: {output_dir}")

    # 🔥 CREATE SHARED RATE LIMITER (one for all combos!)
    shared_rate_limiter = _get_shared_rate_limiter(
        rate=rate_limit * 5,  # 🔥 5x multiplier for burst capacity!
        burst=int(rate_limit * 30)  # Large burst for immediate concurrency
    )

    logger.info(f"🚀 Shared rate limiter: {rate_limit * 5} req/s, burst={int(rate_limit * 30)}")
    
    # Determine mode
    using_pool = cookie_pool is not None
    
    logger.info("=" * 80)
    if using_pool:
        logger.info(f"🚀 MULTI-BROWSER BULK CONCURRENT SCRAPING (Memory-Efficient)")
        logger.info("=" * 80)
        num_browsers = cookie_pool.num_browsers
        logger.info(f"Browsers:      {num_browsers}")
        logger.info(f"Per-browser:   {cookie_pool.max_concurrent_per_browser} concurrent")
        logger.info(f"Total max:     {num_browsers * cookie_pool.max_concurrent_per_browser} concurrent")
    else:
        logger.info(f"🚀 SINGLE-BROWSER BULK CONCURRENT SCRAPING (Memory-Efficient)")
        logger.info("=" * 80)
        logger.info(f"Max concurrent: {max_concurrent}")
    
    logger.info(f"Origins:       {', '.join(origins)}")
    logger.info(f"Destinations:  {', '.join(destinations)}")
    
    # Display date information
    total_dates, consecutive_days = get_date_range_info(dates)
    if consecutive_days > 0:
        logger.info(f"Date range:    {dates[0]} to {dates[-1]} ({total_dates} days)")
    else:
        if total_dates > 3:
            logger.info(f"Dates:        {dates[0]}, {dates[1]}, {dates[2]} ... {dates[-1]} ({total_dates} total)")
        else:
            logger.info(f"Dates:         {', '.join(dates)}")
    
    logger.info(f"Total combos:  {total}")
    logger.info(f"Search types:  {', '.join(search_types)}")
    logger.info(f"💾 Streaming:   Results saved immediately to disk")
    logger.info("=" * 80)
    logger.info("")
    
    # Create appropriate semaphore
    if using_pool:
        semaphore = None
    else:
        semaphore = asyncio.Semaphore(max_concurrent)
    
    # Track statistics
    stats = {
        'successful': 0,
        'failed': 0,
        'total_flights': 0,
        'start_time': asyncio.get_event_loop().time(),
        # 🆕 NEW METRICS
        'total_api_requests': 0,        # Total API calls made
        'total_responses_bytes': 0,     # Data downloaded from API
        'total_saved_bytes': 0,         # Data saved to disk
        'failed_retries': 0,            # Total retry attempts
        'average_response_times': [],    # Response times for averaging
        'cookie_refreshes': 0,          # Times cookies were refreshed
    }
    stats_lock = asyncio.Lock()
    
    async def scrape_and_save_single_combo(task_id: int, origin: str, dest: str, date: str):
        """
        Scrape a single combination and save immediately with metrics tracking.
        This function does NOT return large data - everything is streamed to disk.
        """
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
        
        # 🔥 GET SHARED RESOURCES
        file_io_semaphore = _get_file_io_semaphore(max_concurrent_writes=3)
        
        # Acquire semaphore ONLY for scraping
        async with task_semaphore:
            try:
                browser_prefix = f"[Browser #{browser_id}] " if browser_id is not None else ""
                logger.info(f"{browser_prefix}🔍 Starting: {origin} → {dest} on {date}")
                
                # Track start time
                combo_start = asyncio.get_event_loop().time()
                
                # 🔥 USE SHARED RATE LIMITER
                results, raw_responses, task_metrics = await scrape_flights_with_metrics(
                    origin=origin,
                    destination=dest,
                    date=date,
                    passengers=passengers,
                    cookie_manager=task_cookie_manager,
                    cabin_filter=cabin_filter,
                    search_types=search_types,
                    rate_limiter=shared_rate_limiter,  # 🔥 SHARED!
                    rate_limit=rate_limit,
                )

                
                combo_duration = asyncio.get_event_loop().time() - combo_start
                
                # Update global stats with task metrics
                async with stats_lock:
                    stats['total_api_requests'] += task_metrics['api_requests']
                    stats['total_responses_bytes'] += task_metrics['responses_bytes']
                    stats['failed_retries'] += task_metrics['retries']
                    stats['average_response_times'].extend(task_metrics.get('response_times', []))
                    stats['cookie_refreshes'] += task_metrics.get('cookie_refreshes', 0)
                
            except Exception as e:
                async with stats_lock:
                    stats['failed'] += 1
                
                browser_prefix = f"[Browser #{browser_id}] " if browser_id is not None else ""
                logger.error(f"{browser_prefix}❌ Failed: {origin} → {dest} on {date}: {e}")
                return False
        
        # ✅ SAVE WITH FILE I/O SEMAPHORE - Throttle concurrent disk writes!
        async with file_io_semaphore:
            try:
                save_start = asyncio.get_event_loop().time()
                
                output_file, num_flights, saved_bytes = await save_results_streaming(
                    results,
                    raw_responses,
                    output_dir,
                    origin,
                    dest,
                    date,
                    passengers,
                    cabin_filter,
                )
                
                save_duration = asyncio.get_event_loop().time() - save_start
                
                # Clear memory
                del results
                del raw_responses
                gc.collect()
                
                # Update stats
                async with stats_lock:
                    stats['successful'] += 1
                    stats['total_flights'] += num_flights
                    stats['total_saved_bytes'] += saved_bytes
                
                # Log completion with timing info
                browser_prefix = f"[Browser #{browser_id}] " if browser_id is not None else ""
                logger.success(
                    f"{browser_prefix}✅ Completed: {origin} → {dest} on {date} "
                    f"({len(search_types)} searches, {num_flights} flights saved) "
                    f"API: {combo_duration:.2f}s, Save: {save_duration:.2f}s"
                )
                
                return True
                
            except Exception as e:
                async with stats_lock:
                    stats['failed'] += 1
                
                browser_prefix = f"[Browser #{browser_id}] " if browser_id is not None else ""
                logger.error(f"{browser_prefix}❌ Save failed: {origin} → {dest} on {date}: {e}")
                return False
    
    # Create tasks for all combinations
    tasks = [
        scrape_and_save_single_combo(i, origin, dest, date)
        for i, (origin, dest, date) in enumerate(combinations)
    ]
    
    # Execute all tasks concurrently
    if using_pool:
        logger.info(f"⚡ Executing {total} combinations across {num_browsers} browsers...")
    else:
        logger.info(f"⚡ Executing {total} combinations with max {max_concurrent} concurrent...")
    logger.info("")
    
    # Execute with periodic progress logging
    await asyncio.gather(*tasks, return_exceptions=False)
    
    end_time = asyncio.get_event_loop().time()
    duration = end_time - stats['start_time']
    
    # Calculate final metrics
    req_per_sec = stats['total_api_requests'] / duration if duration > 0 else 0
    
    # Calculate average response time
    avg_response_time = (
        sum(stats['average_response_times']) / len(stats['average_response_times'])
        if stats['average_response_times'] else 0
    )
    
    # Format data sizes helper
    def format_bytes(bytes_val):
        """Format bytes to human readable"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_val < 1024.0:
                return f"{bytes_val:.2f} {unit}"
            bytes_val /= 1024.0
        return f"{bytes_val:.2f} TB"
    
    # Calculate compression ratio
    compression_ratio = (
        stats['total_saved_bytes'] / stats['total_responses_bytes'] * 100
        if stats['total_responses_bytes'] > 0 else 0
    )
    
    # 🆕 ENHANCED SUMMARY OUTPUT
    logger.info("")
    logger.info("=" * 80)
    logger.success("✅ BULK SCRAPING COMPLETE")
    logger.info("=" * 80)
    logger.info("")
    logger.info("📊 RESULTS")
    logger.info(f"   Total combinations:    {total}")
    logger.info(f"   ✅ Successful:         {stats['successful']}")
    logger.info(f"   ❌ Failed:             {stats['failed']}")
    logger.info(f"   ✈️  Total flights:      {stats['total_flights']}")
    logger.info("")
    logger.info("⏱️  PERFORMANCE")
    logger.info(f"   Total duration:        {duration:.1f}s")
    logger.info(f"   Avg per combo:         {duration/total:.2f}s")
    logger.info(f"   Requests/second:       {req_per_sec:.2f} req/s")
    if avg_response_time > 0:
        logger.info(f"   Avg response time:     {avg_response_time*1000:.0f}ms")
    logger.info(f"   Cookie refreshes:      {stats['cookie_refreshes']}")
    logger.info(f"   Failed retries:        {stats['failed_retries']}")
    logger.info("")
    logger.info("💾 DATA TRANSFER")
    logger.info(f"   API requests made:     {stats['total_api_requests']}")
    logger.info(f"   Data downloaded:       {format_bytes(stats['total_responses_bytes'])}")
    logger.info(f"   Data saved to disk:    {format_bytes(stats['total_saved_bytes'])}")
    logger.info(f"   Compression ratio:     {compression_ratio:.1f}%")
    if stats['total_api_requests'] > 0:
        avg_per_req = stats['total_responses_bytes'] / stats['total_api_requests']
        logger.info(f"   Avg per request:       {format_bytes(avg_per_req)}")
    
    if using_pool:
        logger.info("")
        logger.info("🔥 CONCURRENCY")
        logger.info(f"   Browsers used:         {num_browsers}")
        logger.info(f"   Max per browser:       {cookie_pool.max_concurrent_per_browser}")
        logger.info(f"   Total concurrency:     {num_browsers * cookie_pool.max_concurrent_per_browser}")
        logger.info(f"   Effective rate:        {stats['successful']/duration:.2f} combos/sec")
        logger.info("")
        cookie_pool.print_stats()
    else:
        logger.info("")
        logger.info("🔥 CONCURRENCY")
        logger.info(f"   Max concurrent:        {max_concurrent}")
        logger.info(f"   Effective rate:        {stats['successful']/duration:.2f} combos/sec")
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("")
    
    return stats


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

    # Proxy configuration
    proxy_group = parser.add_argument_group("Proxy Configuration")
    proxy_group.add_argument(
        "--proxy-file",
        type=str,
        help="Path to proxy file (format: host:port:username:password per line)"
    )
    proxy_group.add_argument(
        "--proxy-cooldown",
        type=int,
        default=40,
        help="Minutes to wait after proxy IP block (default: 40)"
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

                # Initialize proxy pool if proxy file provided
                proxy_pool = None
                if args.proxy_file:
                    proxy_file = Path(args.proxy_file)
                    if not proxy_file.exists():
                        logger.error(f"Proxy file not found: {proxy_file}")
                        sys.exit(1)
                    
                    try:
                        proxy_pool = ProxyPool(
                            proxy_file=proxy_file,
                            cooldown_minutes=args.proxy_cooldown,
                            max_browsers_per_proxy=3,
                        )
                        logger.info(f"✅ Loaded {len(proxy_pool.proxies)} proxies from {proxy_file}")
                    except Exception as e:
                        logger.error(f"Failed to load proxies: {e}")
                        sys.exit(1)
                
                # Get single proxy if using proxies
                single_proxy = None
                if proxy_pool:
                    single_proxy = await proxy_pool.get_available_proxy()
                    if single_proxy:
                        logger.info(f"Using proxy: {single_proxy.host}:{single_proxy.port}")
                
                # Create cookie manager with optional proxy
                cookie_manager = CookieManager(
                    cookie_file=cookie_file,
                    test_origin=args.test_origin,
                    test_destination=args.test_destination,
                    test_days_ahead=args.test_days_ahead,
                    proxy=single_proxy,
                )
                
                try:
                    await cookie_manager.get_cookies(
                        force_refresh=True,
                        headless=not args.no_headless,
                        wait_time=args.cookie_wait_time,
                    )
                    
                    # Mark proxy success if using
                    if proxy_pool and single_proxy:
                        await proxy_pool.mark_proxy_success(single_proxy)
                        
                    logger.success("Cookie extraction complete!")
                    return
                    
                except Exception as e:
                    # Handle IP blocking
                    
                    if isinstance(e, IPBlockedError) and proxy_pool and single_proxy:
                        logger.error("Proxy got IP blocked during cookie extraction!")
                        await proxy_pool.mark_proxy_blocked(single_proxy)
                    elif proxy_pool and single_proxy:
                        await proxy_pool.mark_proxy_failure(single_proxy)
                    
                    raise

            # Initialize proxy pool if proxy file provided
            proxy_pool = None
            if args.proxy_file:
                proxy_file = Path(args.proxy_file)
                if not proxy_file.exists():
                    logger.error(f"Proxy file not found: {proxy_file}")
                    sys.exit(1)
                
                try:
                    proxy_pool = ProxyPool(
                        proxy_file=proxy_file,
                        cooldown_minutes=args.proxy_cooldown,
                        max_browsers_per_proxy=3,
                    )
                    logger.info(f"✅ Loaded {len(proxy_pool.proxies)} proxies from {proxy_file}")
                except Exception as e:
                    logger.error(f"Failed to load proxies: {e}")
                    sys.exit(1)

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
                
                # Determine browser count based on proxies
                if proxy_pool:
                    # With proxies: one browser can be up to 3 browsers per proxy
                    max_possible_browsers = len(proxy_pool.proxies) * 3
                    requested_browsers = args.browsers
                    
                    if requested_browsers > max_possible_browsers:
                        logger.warning(
                            f"Requested {requested_browsers} browsers but only "
                            f"{max_possible_browsers} supported by {len(proxy_pool.proxies)} proxies"
                        )
                        logger.warning(f"Adjusting to {max_possible_browsers} browsers")
                        num_browsers = max_possible_browsers
                    else:
                        num_browsers = requested_browsers
                    
                    use_multi_browser = num_browsers > 1
                else:
                    # Without proxies: use requested browser count
                    use_multi_browser = args.browsers > 1
                    num_browsers = args.browsers if use_multi_browser else 1
                
                if use_multi_browser:
                    if proxy_pool:
                        logger.info(f"🍪 Multi-browser mode with proxy rotation: {num_browsers} browsers across {len(proxy_pool.proxies)} proxies")
                    else:
                        logger.info(f"🍪 Multi-browser mode: {num_browsers} browsers")
                    
                    # Create cookie pool with proxy support
                    cookie_dir = Path("./cookies")
                    cookie_pool = CookiePool(
                        num_browsers=num_browsers,
                        base_cookie_dir=cookie_dir,
                        max_concurrent_per_browser=args.max_concurrent,
                        test_origin=args.test_origin,
                        test_destination=args.test_destination,
                        test_days_ahead=args.test_days_ahead,
                        proxy_pool=proxy_pool,
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
                        cookie_pool=cookie_pool,
                        cabin_filter=args.cabin,
                        search_types=args.search_type,
                        rate_limit=args.rate_limit,
                        max_concurrent=args.max_concurrent,
                        output_dir=Path(args.output),
                    )
                else:
                    # Single browser mode with optional single proxy
                    logger.info("🍪 Single-browser mode")
                    
                    # Get single proxy if pool available
                    single_proxy = None
                    if proxy_pool:
                        single_proxy = await proxy_pool.get_available_proxy()
                        if single_proxy:
                            logger.info(f"   Using proxy: {single_proxy.host}:{single_proxy.port}")
                    
                    # Create cookie manager with optional proxy
                    cookie_manager = CookieManager(
                        cookie_file=cookie_file,
                        test_origin=args.test_origin,
                        test_destination=args.test_destination,
                        test_days_ahead=args.test_days_ahead,
                        proxy=single_proxy,
                    )
                    
                    # Extract cookies if requested
                    if args.extract_cookies:
                        try:
                            await cookie_manager.get_cookies(
                                force_refresh=True,
                                headless=not args.no_headless,
                                wait_time=args.cookie_wait_time,
                            )
                            
                            # Mark proxy success
                            if proxy_pool and single_proxy:
                                await proxy_pool.mark_proxy_success(single_proxy)
                                
                        except Exception as e:
                            # Handle IP blocking
                            
                            if isinstance(e, IPBlockedError) and proxy_pool and single_proxy:
                                logger.error("Proxy got IP blocked during cookie extraction!")
                                await proxy_pool.mark_proxy_blocked(single_proxy)
                            elif proxy_pool and single_proxy:
                                await proxy_pool.mark_proxy_failure(single_proxy)
                            
                            raise
                    
                    # Run bulk scraping with single cookie manager
                    bulk_results = await scrape_bulk_concurrent(
                        origins=[o.upper() for o in origins],
                        destinations=[d.upper() for d in destinations],
                        dates=dates,
                        passengers=args.passengers,
                        cookie_manager=cookie_manager,
                        cabin_filter=args.cabin,
                        search_types=args.search_type,
                        rate_limit=args.rate_limit,
                        max_concurrent=args.max_concurrent,
                        output_dir=Path(args.output),
                    )
                
                # Final summary
                logger.info("")
                logger.info("=" * 80)
                logger.success(f"✓ Bulk scraping complete! Saved {bulk_results['total_flights']} flights")
                logger.info(f"  Output directory: {args.output}")
                if use_multi_browser:
                    logger.info(f"  Browsers used: {num_browsers}")
                    logger.info(f"  Per-browser concurrency: {args.max_concurrent}")
                    logger.info(f"  Total effective concurrency: {num_browsers * args.max_concurrent}")
                if proxy_pool:
                    logger.info(f"  Proxies used: {len(proxy_pool.proxies)}")
                    logger.info("")
                    proxy_pool.print_stats()
                logger.info("=" * 80)
                
            else:
                # Single-route mode
                if not all([origins and len(origins) == 1, destinations and len(destinations) == 1, dates and len(dates) == 1]):
                    logger.error("Please specify exactly one origin, one destination, and one date.")
                    sys.exit(1)

                # Get single proxy if pool available
                single_proxy = None
                if proxy_pool:
                    single_proxy = await proxy_pool.get_available_proxy()
                    if single_proxy:
                        logger.info(f"Using proxy: {single_proxy.host}:{single_proxy.port}")

                # Create cookie manager with optional proxy
                cookie_manager = CookieManager(
                    cookie_file=cookie_file,
                    test_origin=args.test_origin,
                    test_destination=args.test_destination,
                    test_days_ahead=args.test_days_ahead,
                    proxy=single_proxy,
                )

                # Extract cookies if requested
                if args.extract_cookies:
                    try:
                        await cookie_manager.get_cookies(
                            force_refresh=True,
                            headless=not args.no_headless,
                            wait_time=args.cookie_wait_time,
                        )
                        
                        # Mark proxy success
                        if proxy_pool and single_proxy:
                            await proxy_pool.mark_proxy_success(single_proxy)
                            
                    except Exception as e:
                        # Handle IP blocking
                        
                        if isinstance(e, IPBlockedError) and proxy_pool and single_proxy:
                            logger.error("Proxy got IP blocked during cookie extraction!")
                            await proxy_pool.mark_proxy_blocked(single_proxy)
                        elif proxy_pool and single_proxy:
                            await proxy_pool.mark_proxy_failure(single_proxy)
                        
                        raise

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

                # Mark proxy success/failure
                if proxy_pool and single_proxy:
                    if any(results.values()):
                        await proxy_pool.mark_proxy_success(single_proxy)
                    else:
                        await proxy_pool.mark_proxy_failure(single_proxy)

                # Check results
                if not any(results.values()):
                    logger.error("All searches failed")
                    sys.exit(1)

                # Save results using async streaming storage
                output_dir = Path(args.output)
                output_file, num_flights = await save_results_streaming(
                    results,
                    raw_responses,
                    output_dir,
                    origin,
                    destination,
                    date,
                    args.passengers,
                    args.cabin,
                )

                # Clear memory
                del results
                del raw_responses
                gc.collect()

                # Summary
                logger.info("")
                logger.info("=" * 60)
                logger.success("✓ Scraping complete!")
                logger.info(f"  Route: {origin} → {destination}")
                logger.info(f"  Date: {date}")
                logger.info(f"  Cabin: {CABIN_CLASS_MAP.get(args.cabin, args.cabin.lower())}")
                logger.info(f"  Flights saved: {num_flights}")
                logger.info(f"  Output: {output_dir}")
                if proxy_pool and single_proxy:
                    logger.info(f"  Proxy: {single_proxy.host}:{single_proxy.port}")
                logger.info("=" * 60)

        except IPBlockedError as e:
            logger.error("")
            logger.error("=" * 80)
            logger.error("🚫 IP ADDRESS BLOCKED BY SERVER")
            logger.error("=" * 80)
            logger.error("")
            logger.error("❌ Your IP address has been blocked by the server.")
            logger.error("   This is a SERVER-LEVEL block, not an Akamai challenge.")
            logger.error("")
            logger.error("📋 Details:")
            logger.error(f"   {str(e)}")
            logger.error("")
            logger.error("⏰ TIMING RECOMMENDATIONS:")
            logger.error("   • Minimum wait: ~20 minutes")
            logger.error("   • Recommended wait: ~40 minutes (safer)")
            logger.error("   • Warning: Retrying at ~20 minutes may cause instant re-blocking")
            logger.error("")
            logger.error("💡 WHAT TO DO:")
            logger.error("   1. Wait at least 40 minutes before retrying")
            logger.error("   2. Consider using a different IP address or proxy")
            logger.error("   3. Reduce scraping aggressiveness when you retry")
            logger.error("   4. Check if you have other processes hitting the same site")
            logger.error("")
            logger.error("🔍 TROUBLESHOOTING:")
            logger.error("   • Review your recent request patterns")
            logger.error("   • Ensure you're not making too many concurrent requests")
            logger.error("   • Verify your rate limits are appropriately configured")
            logger.error("   • Consider using --rate-limit 0.5 or lower")
            logger.error("")
            logger.error("=" * 80)
            sys.exit(2)
            
        except KeyboardInterrupt:
            logger.warning("Interrupted by user")
            sys.exit(1)
        except Exception as e:
            logger.exception(f"Fatal error: {e}")
            sys.exit(1)

    asyncio.run(run())


if __name__ == "__main__":
    main()