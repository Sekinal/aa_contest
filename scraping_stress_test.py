"""
Aggressive async stress test for AA scraper
Makes 500 concurrent requests to test blocking thresholds
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from collections import defaultdict

from loguru import logger
from tqdm.asyncio import tqdm

from aa_scraper import AAFlightClient, CookieManager, AdaptiveRateLimiter
from aa_scraper.config import DEFAULT_COOKIE_FILE
from aa_scraper.exceptions import CookieExpiredError, RateLimitError


# Test routes for variety
TEST_ROUTES = [
    ("LAX", "JFK"), ("ORD", "MIA"), ("DFW", "LAX"), ("PHX", "DCA"),
    ("BOS", "SFO"), ("ATL", "SEA"), ("DEN", "MCO"), ("LAS", "EWR"),
    ("SAN", "IAH"), ("PDX", "CLT"), ("MSP", "FLL"), ("DTW", "PHX"),
    ("SLC", "BOS"), ("AUS", "ORD"), ("RDU", "DFW"),
]


@dataclass
class RequestResult:
    """Individual request result"""
    request_id: int
    origin: str
    destination: str
    status: str  # success, failed, blocked, rate_limited
    response_time: float
    error_message: Optional[str] = None
    timestamp: str = None
    http_status: Optional[int] = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


class AggressiveStressTest:
    """Aggressive concurrent stress test runner"""
    
    def __init__(
        self,
        total_requests: int = 500,
        max_concurrent: int = 20,
        rate_limit: float = 5.0,
        burst: int = 20,
    ):
        """
        Initialize stress test.
        
        Args:
            total_requests: Total number of requests to make
            max_concurrent: Maximum concurrent requests
            rate_limit: Rate limit in requests per second
            burst: Burst capacity for rate limiter
        """
        self.total_requests = total_requests
        self.max_concurrent = max_concurrent
        self.rate_limit = rate_limit
        self.burst = burst
        
        self.results: List[RequestResult] = []
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        
        # Real-time statistics
        self.success_count = 0
        self.failure_count = 0
        self.blocked_count = 0
        self.rate_limited_count = 0
        
        # Track status codes
        self.status_codes = defaultdict(int)
        
        # Semaphore for concurrency control
        self.semaphore = asyncio.Semaphore(max_concurrent)
        
        logger.info(f"Initialized aggressive stress test:")
        logger.info(f"  Total requests: {total_requests}")
        logger.info(f"  Max concurrent: {max_concurrent}")
        logger.info(f"  Rate limit: {rate_limit} req/s")
        logger.info(f"  Burst capacity: {burst}")
    
    async def make_request(
        self,
        request_id: int,
        client: AAFlightClient,
        origin: str,
        destination: str,
        date: str,
    ) -> RequestResult:
        """Make a single request with semaphore control"""
        async with self.semaphore:
            start = time.time()
            
            try:
                response = await client.search_flights(
                    origin=origin,
                    destination=destination,
                    date=date,
                    passengers=1,
                    search_type="Award"
                )
                
                elapsed = time.time() - start
                
                if response:
                    self.success_count += 1
                    self.status_codes[200] += 1
                    return RequestResult(
                        request_id=request_id,
                        origin=origin,
                        destination=destination,
                        status="success",
                        response_time=elapsed,
                        http_status=200
                    )
                else:
                    self.failure_count += 1
                    return RequestResult(
                        request_id=request_id,
                        origin=origin,
                        destination=destination,
                        status="failed",
                        response_time=elapsed,
                        error_message="Empty response"
                    )
                    
            except CookieExpiredError as e:
                elapsed = time.time() - start
                self.blocked_count += 1
                self.status_codes[403] += 1
                return RequestResult(
                    request_id=request_id,
                    origin=origin,
                    destination=destination,
                    status="blocked",
                    response_time=elapsed,
                    error_message=str(e),
                    http_status=403
                )
                
            except RateLimitError as e:
                elapsed = time.time() - start
                self.rate_limited_count += 1
                self.status_codes[429] += 1
                return RequestResult(
                    request_id=request_id,
                    origin=origin,
                    destination=destination,
                    status="rate_limited",
                    response_time=elapsed,
                    error_message=str(e),
                    http_status=429
                )
                
            except Exception as e:
                elapsed = time.time() - start
                self.failure_count += 1
                
                # Try to extract status code from error
                http_status = None
                if "403" in str(e):
                    http_status = 403
                    self.status_codes[403] += 1
                elif "429" in str(e):
                    http_status = 429
                    self.status_codes[429] += 1
                else:
                    self.status_codes["error"] += 1
                
                return RequestResult(
                    request_id=request_id,
                    origin=origin,
                    destination=destination,
                    status="failed",
                    response_time=elapsed,
                    error_message=str(e),
                    http_status=http_status
                )
    
    async def run(
        self,
        cookie_manager: CookieManager,
        extract_cookies: bool = False,
        headless: bool = True
    ) -> Dict:
        """Run the aggressive stress test"""
        logger.info("")
        logger.info("=" * 80)
        logger.info("🔥 AGGRESSIVE ASYNC STRESS TEST 🔥")
        logger.info("=" * 80)
        logger.info(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("")
        
        # Extract cookies if requested
        if extract_cookies:
            logger.info("⏳ Extracting fresh cookies...")
            try:
                await cookie_manager.get_cookies(
                    force_refresh=True,
                    headless=headless,
                    wait_time=15
                )
                logger.success("✅ Cookies extracted successfully")
            except Exception as e:
                logger.error(f"❌ Cookie extraction failed: {e}")
                return {"error": "Cookie extraction failed"}
        
        # Initialize client with aggressive settings
        rate_limiter = AdaptiveRateLimiter(rate=self.rate_limit, burst=self.burst)
        client = AAFlightClient(cookie_manager, rate_limiter, timeout=30.0)
        
        # Generate test data
        logger.info("📋 Generating test requests...")
        test_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        
        tasks = []
        for i in range(self.total_requests):
            # Rotate through routes
            origin, destination = TEST_ROUTES[i % len(TEST_ROUTES)]
            
            task = self.make_request(
                request_id=i,
                client=client,
                origin=origin,
                destination=destination,
                date=test_date
            )
            tasks.append(task)
        
        logger.info(f"✅ Generated {len(tasks)} requests across {len(TEST_ROUTES)} routes")
        logger.info("")
        logger.info("🚀 Starting aggressive concurrent execution...")
        logger.info("")
        
        # Execute all requests concurrently with progress bar
        self.start_time = time.time()
        
        # Use tqdm for progress tracking
        results = []
        for coro in tqdm.as_completed(
            tasks,
            total=len(tasks),
            desc="Requests",
            unit="req",
            colour="green"
        ):
            result = await coro
            results.append(result)
            
            # Real-time status updates every 50 requests
            if len(results) % 50 == 0:
                self._print_live_stats(len(results))
        
        self.end_time = time.time()
        self.results = results
        
        # Final summary
        logger.info("")
        logger.info("=" * 80)
        logger.info("✅ TEST COMPLETE")
        logger.info("=" * 80)
        
        summary = self.get_summary()
        self._print_summary(summary)
        
        # Save results
        self._save_results(summary)
        
        return summary
    
    def _print_live_stats(self, completed: int):
        """Print live statistics during test"""
        elapsed = time.time() - self.start_time
        rate = completed / elapsed if elapsed > 0 else 0
        success_rate = (self.success_count / completed * 100) if completed > 0 else 0
        
        logger.info(f"Progress: {completed}/{self.total_requests} | "
                   f"Rate: {rate:.1f} req/s | "
                   f"Success: {success_rate:.1f}% | "
                   f"❌403: {self.blocked_count} | "
                   f"⏰429: {self.rate_limited_count}")
    
    def get_summary(self) -> Dict:
        """Generate comprehensive summary"""
        duration = self.end_time - self.start_time
        
        # Response time statistics
        response_times = [r.response_time for r in self.results]
        avg_response_time = sum(response_times) / len(response_times) if response_times else 0
        min_response_time = min(response_times) if response_times else 0
        max_response_time = max(response_times) if response_times else 0
        
        # Calculate percentiles
        sorted_times = sorted(response_times)
        p50 = sorted_times[len(sorted_times) // 2] if sorted_times else 0
        p95 = sorted_times[int(len(sorted_times) * 0.95)] if sorted_times else 0
        p99 = sorted_times[int(len(sorted_times) * 0.99)] if sorted_times else 0
        
        # Success rate
        total = len(self.results)
        success_rate = (self.success_count / total * 100) if total > 0 else 0
        
        # Effective throughput
        effective_rate = self.success_count / duration if duration > 0 else 0
        
        # Status code breakdown
        status_breakdown = dict(self.status_codes)
        
        # Blocking analysis
        blocking_percentage = (self.blocked_count / total * 100) if total > 0 else 0
        rate_limit_percentage = (self.rate_limited_count / total * 100) if total > 0 else 0
        
        # Time-based analysis
        blocked_over_time = []
        window_size = 50
        for i in range(0, len(self.results), window_size):
            window = self.results[i:i+window_size]
            blocked_in_window = sum(1 for r in window if r.status == "blocked")
            blocked_over_time.append({
                "request_range": f"{i}-{i+len(window)}",
                "blocked_count": blocked_in_window,
                "blocked_percentage": (blocked_in_window / len(window) * 100) if window else 0
            })
        
        return {
            "test_config": {
                "total_requests": self.total_requests,
                "max_concurrent": self.max_concurrent,
                "rate_limit": self.rate_limit,
                "burst": self.burst,
            },
            "execution": {
                "duration_seconds": round(duration, 2),
                "start_time": datetime.fromtimestamp(self.start_time).isoformat(),
                "end_time": datetime.fromtimestamp(self.end_time).isoformat(),
            },
            "results": {
                "total_requests": total,
                "successful": self.success_count,
                "failed": self.failure_count,
                "blocked_403": self.blocked_count,
                "rate_limited_429": self.rate_limited_count,
                "success_rate_percent": round(success_rate, 2),
                "blocking_percentage": round(blocking_percentage, 2),
                "rate_limit_percentage": round(rate_limit_percentage, 2),
            },
            "performance": {
                "effective_throughput_req_per_sec": round(effective_rate, 2),
                "avg_response_time_sec": round(avg_response_time, 2),
                "min_response_time_sec": round(min_response_time, 2),
                "max_response_time_sec": round(max_response_time, 2),
                "p50_response_time_sec": round(p50, 2),
                "p95_response_time_sec": round(p95, 2),
                "p99_response_time_sec": round(p99, 2),
            },
            "status_codes": status_breakdown,
            "blocking_pattern": blocked_over_time,
            "assessment": self._get_assessment(blocking_percentage, rate_limit_percentage, success_rate),
        }
    
    def _get_assessment(self, blocking_pct: float, rate_limit_pct: float, success_rate: float) -> Dict:
        """Assess the test results and provide recommendations"""
        if blocking_pct >= 20:
            severity = "CRITICAL"
            status = "🔴 HEAVILY BLOCKED"
            message = "System is heavily blocked. Bot detection is active."
            recommendations = [
                "Significantly reduce request rate (try 0.5 req/s)",
                "Increase delays between requests (5-10s)",
                "Implement request randomization",
                "Review cookie extraction process",
                "Consider using residential proxies",
            ]
        elif blocking_pct >= 10:
            severity = "HIGH"
            status = "🟠 MODERATE BLOCKING"
            message = "Significant blocking detected. Approaching detection threshold."
            recommendations = [
                "Reduce request rate by 50%",
                "Add random delays (2-5s)",
                "Limit concurrent connections to 5-10",
                "Refresh cookies more frequently",
            ]
        elif blocking_pct >= 5:
            severity = "MEDIUM"
            status = "🟡 LIGHT BLOCKING"
            message = "Some blocking detected. Monitor closely."
            recommendations = [
                "Reduce request rate by 25%",
                "Add small random delays (1-2s)",
                "Monitor blocking patterns over time",
            ]
        elif rate_limit_pct >= 10:
            severity = "MEDIUM"
            status = "⏰ RATE LIMITED"
            message = "Hitting rate limits frequently."
            recommendations = [
                "Reduce request rate to stay under limits",
                "Implement exponential backoff",
                "Respect Retry-After headers",
            ]
        elif success_rate >= 95:
            severity = "GOOD"
            status = "✅ HEALTHY"
            message = "System operating within acceptable parameters."
            recommendations = [
                "Current settings appear safe",
                "Monitor for changes over time",
                "Consider slight optimizations if needed",
            ]
        else:
            severity = "MEDIUM"
            status = "⚠️ MARGINAL"
            message = "Success rate lower than expected."
            recommendations = [
                "Review error patterns",
                "Check cookie validity",
                "Verify network stability",
            ]
        
        return {
            "severity": severity,
            "status": status,
            "message": message,
            "recommendations": recommendations,
        }
    
    def _print_summary(self, summary: Dict):
        """Print formatted summary to console"""
        logger.info("")
        logger.info("📊 TEST RESULTS")
        logger.info("-" * 80)
        
        # Results
        results = summary["results"]
        logger.info(f"Total Requests:      {results['total_requests']}")
        logger.info(f"✅ Successful:       {results['successful']} ({results['success_rate_percent']}%)")
        logger.info(f"❌ Failed:           {results['failed']}")
        logger.info(f"🚫 Blocked (403):    {results['blocked_403']} ({results['blocking_percentage']}%)")
        logger.info(f"⏰ Rate Limited:     {results['rate_limited_429']} ({results['rate_limit_percentage']}%)")
        logger.info("")
        
        # Performance
        perf = summary["performance"]
        logger.info("⚡ PERFORMANCE")
        logger.info("-" * 80)
        logger.info(f"Duration:            {summary['execution']['duration_seconds']}s")
        logger.info(f"Throughput:          {perf['effective_throughput_req_per_sec']} req/s")
        logger.info(f"Avg Response Time:   {perf['avg_response_time_sec']}s")
        logger.info(f"P50 Response Time:   {perf['p50_response_time_sec']}s")
        logger.info(f"P95 Response Time:   {perf['p95_response_time_sec']}s")
        logger.info(f"P99 Response Time:   {perf['p99_response_time_sec']}s")
        logger.info("")
        
        # Assessment
        assessment = summary["assessment"]
        logger.info("🎯 ASSESSMENT")
        logger.info("-" * 80)
        logger.info(f"Status: {assessment['status']}")
        logger.info(f"Severity: {assessment['severity']}")
        logger.info(f"Message: {assessment['message']}")
        logger.info("")
        logger.info("Recommendations:")
        for i, rec in enumerate(assessment['recommendations'], 1):
            logger.info(f"  {i}. {rec}")
        logger.info("")
        
        # Status codes
        logger.info("📈 STATUS CODE BREAKDOWN")
        logger.info("-" * 80)
        for code, count in sorted(summary['status_codes'].items()):
            logger.info(f"  {code}: {count}")
        logger.info("=" * 80)
    
    def _save_results(self, summary: Dict):
        """Save detailed results to file"""
        output_dir = Path("./stress_test_results")
        output_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Save summary
        summary_file = output_dir / f"aggressive_test_{timestamp}_summary.json"
        summary_file.write_text(json.dumps(summary, indent=2))
        
        # Save detailed results
        detailed_results = [asdict(r) for r in self.results]
        details_file = output_dir / f"aggressive_test_{timestamp}_details.json"
        details_file.write_text(json.dumps(detailed_results, indent=2))
        
        logger.info(f"💾 Results saved:")
        logger.info(f"   Summary: {summary_file}")
        logger.info(f"   Details: {details_file}")


async def main():
    """Main entry point"""
    import argparse
    import sys
    
    parser = argparse.ArgumentParser(
        description="Aggressive async stress test (500 requests)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run with fresh cookies
  python aggressive_stress_test.py --extract-cookies
  
  # Custom request count
  python aggressive_stress_test.py --requests 1000 --concurrent 50
  
  # More aggressive settings
  python aggressive_stress_test.py --rate 10 --burst 30 --concurrent 30
        """
    )
    
    parser.add_argument(
        "--requests",
        type=int,
        default=500,
        help="Total number of requests (default: 500)"
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=20,
        help="Max concurrent requests (default: 20)"
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=5.0,
        help="Rate limit in req/s (default: 5.0)"
    )
    parser.add_argument(
        "--burst",
        type=int,
        default=20,
        help="Burst capacity (default: 20)"
    )
    parser.add_argument(
        "--extract-cookies",
        action="store_true",
        help="Extract fresh cookies before test"
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser in visible mode"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    from aa_scraper.logging_config import setup_logging
    log_file = Path("./logs/aggressive_stress_test.log")
    setup_logging(verbose=args.verbose, log_file=log_file)
    
    # Initialize test
    test = AggressiveStressTest(
        total_requests=args.requests,
        max_concurrent=args.concurrent,
        rate_limit=args.rate,
        burst=args.burst,
    )
    
    # Initialize cookie manager
    cookie_manager = CookieManager(DEFAULT_COOKIE_FILE)
    
    # Run test
    try:
        await test.run(
            cookie_manager=cookie_manager,
            extract_cookies=args.extract_cookies,
            headless=not args.no_headless
        )
    except KeyboardInterrupt:
        logger.warning("Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Test failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())