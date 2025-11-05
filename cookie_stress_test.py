"""
Cookie extraction stress test
Tests repeated browser automation and cookie extraction to detect blocking patterns
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

from loguru import logger
from tqdm.asyncio import tqdm

from aa_scraper import CookieManager
from aa_scraper.config import DEFAULT_COOKIE_FILE
from aa_scraper.exceptions import CookieExpiredError


@dataclass
class CookieExtractionResult:
    """Result of a single cookie extraction attempt"""
    attempt_id: int
    status: str  # success, failed, blocked, akamai_challenge, timeout
    duration_seconds: float
    cookies_extracted: int
    critical_cookies_present: bool
    important_cookies_present: bool
    bot_defense_cookies_present: bool
    akamai_challenge_detected: bool
    akamai_challenge_passed: bool
    error_message: Optional[str] = None
    timestamp: str = None
    cookie_details: Optional[Dict] = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


class CookieStressTest:
    """Stress test for cookie extraction"""
    
    # Essential cookie categories
    CRITICAL_COOKIES = ["XSRF-TOKEN", "spa_session_id"]
    IMPORTANT_COOKIES = ["JSESSIONID", "_abck", "bm_sv"]
    BOT_DEFENSE_COOKIES = ["bm_sz", "ak_bmsc", "bm_s", "sec_cpt"]
    
    def __init__(
        self,
        total_extractions: int = 20,
        delay_between: float = 5.0,
        headless: bool = True,
        wait_time: int = 15,
        test_origin: str = "SRQ",
        test_destination: str = "BFL",
    ):
        """
        Initialize cookie stress test.
        
        Args:
            total_extractions: Number of cookie extractions to attempt
            delay_between: Seconds to wait between extractions
            headless: Run browser in headless mode
            wait_time: Seconds to wait for API response during extraction
            test_origin: Origin airport for test flight
            test_destination: Destination airport for test flight
        """
        self.total_extractions = total_extractions
        self.delay_between = delay_between
        self.headless = headless
        self.wait_time = wait_time
        self.test_origin = test_origin
        self.test_destination = test_destination
        
        self.results: List[CookieExtractionResult] = []
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        
        # Statistics
        self.success_count = 0
        self.failure_count = 0
        self.akamai_challenges_encountered = 0
        self.akamai_challenges_passed = 0
        self.blocked_count = 0
        
        logger.info(f"Initialized cookie stress test:")
        logger.info(f"  Total extractions: {total_extractions}")
        logger.info(f"  Delay between: {delay_between}s")
        logger.info(f"  Headless mode: {headless}")
        logger.info(f"  Wait time: {wait_time}s")
        logger.info(f"  Test route: {test_origin} → {test_destination}")
    
    def _validate_cookies(self, cookies: Dict[str, str]) -> Tuple[bool, bool, bool, Dict]:
        """
        Validate extracted cookies.
        
        Returns:
            (critical_present, important_present, bot_defense_present, details)
        """
        critical_found = [c for c in self.CRITICAL_COOKIES if c in cookies]
        important_found = [c for c in self.IMPORTANT_COOKIES if c in cookies]
        bot_defense_found = [c for c in self.BOT_DEFENSE_COOKIES if c in cookies]
        
        critical_present = len(critical_found) == len(self.CRITICAL_COOKIES)
        important_present = len(important_found) > 0
        bot_defense_present = len(bot_defense_found) > 0
        
        details = {
            "total_cookies": len(cookies),
            "critical_cookies": critical_found,
            "important_cookies": important_found,
            "bot_defense_cookies": bot_defense_found,
            "missing_critical": [c for c in self.CRITICAL_COOKIES if c not in cookies],
            "cookie_names": list(cookies.keys()),
        }
        
        return critical_present, important_present, bot_defense_present, details
    
    async def extract_single(self, attempt_id: int, cookie_manager: CookieManager) -> CookieExtractionResult:
        """Perform a single cookie extraction"""
        logger.info(f"\n{'='*60}")
        logger.info(f"🍪 Extraction #{attempt_id + 1}/{self.total_extractions}")
        logger.info(f"{'='*60}")
        
        start = time.time()
        
        try:
            # Force fresh extraction
            cookies, headers, referer = await cookie_manager.get_cookies(
                force_refresh=True,
                headless=self.headless,
                wait_time=self.wait_time
            )
            
            duration = time.time() - start
            
            # Validate cookies
            critical_ok, important_ok, bot_defense_ok, cookie_details = self._validate_cookies(cookies)
            
            # Check if we got valid cookies
            if not critical_ok:
                self.failure_count += 1
                return CookieExtractionResult(
                    attempt_id=attempt_id,
                    status="failed",
                    duration_seconds=duration,
                    cookies_extracted=len(cookies),
                    critical_cookies_present=False,
                    important_cookies_present=important_ok,
                    bot_defense_cookies_present=bot_defense_ok,
                    akamai_challenge_detected=False,
                    akamai_challenge_passed=False,
                    error_message=f"Missing critical cookies: {cookie_details['missing_critical']}",
                    cookie_details=cookie_details
                )
            
            # Success!
            self.success_count += 1
            
            logger.success(f"✅ Extraction successful in {duration:.2f}s")
            logger.info(f"   Cookies: {len(cookies)}")
            logger.info(f"   Critical: {'✓' if critical_ok else '✗'}")
            logger.info(f"   Important: {'✓' if important_ok else '✗'}")
            logger.info(f"   Bot Defense: {'✓' if bot_defense_ok else '✗'}")
            
            return CookieExtractionResult(
                attempt_id=attempt_id,
                status="success",
                duration_seconds=duration,
                cookies_extracted=len(cookies),
                critical_cookies_present=critical_ok,
                important_cookies_present=important_ok,
                bot_defense_cookies_present=bot_defense_ok,
                akamai_challenge_detected=False,  # Would be set in cookie_manager
                akamai_challenge_passed=True,
                cookie_details=cookie_details
            )
            
        except CookieExpiredError as e:
            duration = time.time() - start
            self.failure_count += 1
            
            error_msg = str(e).lower()
            
            # Detect different failure types
            if "akamai" in error_msg or "challenge" in error_msg:
                self.akamai_challenges_encountered += 1
                status = "akamai_challenge"
                logger.error(f"🛡️ Akamai challenge failed: {e}")
            elif "403" in error_msg or "forbidden" in error_msg:
                self.blocked_count += 1
                status = "blocked"
                logger.error(f"🚫 Blocked (403): {e}")
            elif "timeout" in error_msg:
                status = "timeout"
                logger.error(f"⏰ Timeout: {e}")
            else:
                status = "failed"
                logger.error(f"❌ Extraction failed: {e}")
            
            return CookieExtractionResult(
                attempt_id=attempt_id,
                status=status,
                duration_seconds=duration,
                cookies_extracted=0,
                critical_cookies_present=False,
                important_cookies_present=False,
                bot_defense_cookies_present=False,
                akamai_challenge_detected="akamai" in error_msg,
                akamai_challenge_passed=False,
                error_message=str(e)
            )
            
        except Exception as e:
            duration = time.time() - start
            self.failure_count += 1
            
            logger.error(f"❌ Unexpected error: {e}")
            
            return CookieExtractionResult(
                attempt_id=attempt_id,
                status="failed",
                duration_seconds=duration,
                cookies_extracted=0,
                critical_cookies_present=False,
                important_cookies_present=False,
                bot_defense_cookies_present=False,
                akamai_challenge_detected=False,
                akamai_challenge_passed=False,
                error_message=str(e)
            )
    
    async def run(self) -> Dict:
        """Run the cookie extraction stress test"""
        logger.info("")
        logger.info("=" * 80)
        logger.info("🍪 COOKIE EXTRACTION STRESS TEST 🍪")
        logger.info("=" * 80)
        logger.info(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("")
        
        self.start_time = time.time()
        
        # Create a unique cookie file for this test
        test_cookie_file = Path(f"./cookies/stress_test_{int(time.time())}.json")
        test_cookie_file.parent.mkdir(parents=True, exist_ok=True)
        
        cookie_manager = CookieManager(
            cookie_file=test_cookie_file,
            test_origin=self.test_origin,
            test_destination=self.test_destination,
            test_days_ahead=7
        )
        
        # Run extractions sequentially
        for i in range(self.total_extractions):
            result = await self.extract_single(i, cookie_manager)
            self.results.append(result)
            
            # Print progress
            success_rate = (self.success_count / (i + 1) * 100)
            logger.info(f"\nProgress: {i+1}/{self.total_extractions} | "
                       f"Success Rate: {success_rate:.1f}% | "
                       f"Akamai: {self.akamai_challenges_encountered} | "
                       f"Blocked: {self.blocked_count}")
            
            # Check if we should stop early
            if self.blocked_count >= 3:
                logger.error("🛑 STOPPING: Too many blocks detected!")
                break
            
            if self.akamai_challenges_encountered >= 5 and self.akamai_challenges_passed == 0:
                logger.error("🛑 STOPPING: Failing all Akamai challenges!")
                break
            
            # Delay between extractions (except last one)
            if i < self.total_extractions - 1:
                logger.info(f"⏳ Waiting {self.delay_between}s before next extraction...")
                await asyncio.sleep(self.delay_between)
        
        self.end_time = time.time()
        
        # Generate summary
        logger.info("")
        logger.info("=" * 80)
        logger.info("✅ TEST COMPLETE")
        logger.info("=" * 80)
        
        summary = self.get_summary()
        self._print_summary(summary)
        
        # Save results
        self._save_results(summary)
        
        # Cleanup test cookie file
        try:
            test_cookie_file.unlink(missing_ok=True)
            (test_cookie_file.parent / f"{test_cookie_file.stem}_headers.json").unlink(missing_ok=True)
            (test_cookie_file.parent / f"{test_cookie_file.stem}_referer.txt").unlink(missing_ok=True)
        except:
            pass
        
        return summary
    
    def get_summary(self) -> Dict:
        """Generate comprehensive summary"""
        duration = self.end_time - self.start_time
        total = len(self.results)
        
        # Success metrics
        success_rate = (self.success_count / total * 100) if total > 0 else 0
        failure_rate = (self.failure_count / total * 100) if total > 0 else 0
        
        # Duration statistics
        durations = [r.duration_seconds for r in self.results]
        avg_duration = sum(durations) / len(durations) if durations else 0
        min_duration = min(durations) if durations else 0
        max_duration = max(durations) if durations else 0
        
        # Cookie quality over time
        cookie_quality = []
        for result in self.results:
            cookie_quality.append({
                "attempt": result.attempt_id + 1,
                "status": result.status,
                "cookies_count": result.cookies_extracted,
                "critical_ok": result.critical_cookies_present,
                "important_ok": result.important_cookies_present,
                "bot_defense_ok": result.bot_defense_cookies_present,
            })
        
        # Status breakdown
        status_counts = {}
        for result in self.results:
            status_counts[result.status] = status_counts.get(result.status, 0) + 1
        
        # Akamai analysis
        akamai_pass_rate = 0
        if self.akamai_challenges_encountered > 0:
            akamai_pass_rate = (self.akamai_challenges_passed / self.akamai_challenges_encountered * 100)
        
        # Pattern analysis - are failures increasing?
        window_size = 5
        failure_trend = []
        for i in range(0, len(self.results), window_size):
            window = self.results[i:i+window_size]
            failed_in_window = sum(1 for r in window if r.status != "success")
            failure_trend.append({
                "extraction_range": f"{i+1}-{i+len(window)}",
                "failures": failed_in_window,
                "failure_rate": (failed_in_window / len(window) * 100) if window else 0
            })
        
        return {
            "test_config": {
                "total_extractions": self.total_extractions,
                "delay_between_seconds": self.delay_between,
                "headless": self.headless,
                "wait_time_seconds": self.wait_time,
                "test_route": f"{self.test_origin} → {self.test_destination}",
            },
            "execution": {
                "duration_seconds": round(duration, 2),
                "extractions_completed": total,
                "start_time": datetime.fromtimestamp(self.start_time).isoformat(),
                "end_time": datetime.fromtimestamp(self.end_time).isoformat(),
            },
            "results": {
                "successful": self.success_count,
                "failed": self.failure_count,
                "blocked": self.blocked_count,
                "success_rate_percent": round(success_rate, 2),
                "failure_rate_percent": round(failure_rate, 2),
            },
            "akamai": {
                "challenges_encountered": self.akamai_challenges_encountered,
                "challenges_passed": self.akamai_challenges_passed,
                "pass_rate_percent": round(akamai_pass_rate, 2) if self.akamai_challenges_encountered > 0 else None,
            },
            "performance": {
                "avg_extraction_time_seconds": round(avg_duration, 2),
                "min_extraction_time_seconds": round(min_duration, 2),
                "max_extraction_time_seconds": round(max_duration, 2),
                "extractions_per_minute": round(60 / avg_duration, 2) if avg_duration > 0 else 0,
            },
            "status_breakdown": status_counts,
            "cookie_quality_over_time": cookie_quality,
            "failure_trend": failure_trend,
            "assessment": self._get_assessment(success_rate, self.blocked_count, self.akamai_challenges_encountered, failure_trend),
        }
    
    def _get_assessment(
        self,
        success_rate: float,
        blocked_count: int,
        akamai_count: int,
        failure_trend: List[Dict]
    ) -> Dict:
        """Assess test results and provide recommendations"""
        
        # Check if failures are increasing
        increasing_failures = False
        if len(failure_trend) >= 2:
            recent_failures = sum(t["failures"] for t in failure_trend[-2:])
            early_failures = sum(t["failures"] for t in failure_trend[:2])
            increasing_failures = recent_failures > early_failures
        
        if blocked_count >= 5:
            severity = "CRITICAL"
            status = "🔴 HEAVILY BLOCKED"
            message = "Cookie extraction is being blocked. Bot detection is active."
            recommendations = [
                "Your browser fingerprint may be flagged",
                "Try using non-headless mode",
                "Increase wait times (20-30s)",
                "Add more random human-like behaviors",
                "Consider rotating user agents or using residential proxies",
                "Reduce extraction frequency significantly",
            ]
        elif blocked_count >= 3:
            severity = "HIGH"
            status = "🟠 MODERATE BLOCKING"
            message = "Repeated cookie extraction is triggering detection."
            recommendations = [
                "Reduce extraction frequency",
                "Increase delays between extractions (30s+)",
                "Try non-headless mode for better evasion",
                "Add more wait time for page loads",
            ]
        elif akamai_count >= 5:
            severity = "HIGH"
            status = "🛡️ AKAMAI CHALLENGES"
            message = "Frequent Akamai challenges detected."
            recommendations = [
                "Your browser automation is being detected",
                "Try non-headless mode",
                "Increase wait times significantly",
                "Add random mouse movements and scrolling",
                "Consider using stealth plugins",
            ]
        elif increasing_failures:
            severity = "MEDIUM"
            status = "📈 DEGRADING PERFORMANCE"
            message = "Success rate decreasing over time."
            recommendations = [
                "Detection may be pattern-based",
                "Add more variation to timing",
                "Limit consecutive extractions",
                "Implement cooling-off periods",
            ]
        elif success_rate >= 90:
            severity = "GOOD"
            status = "✅ HEALTHY"
            message = "Cookie extraction is working well."
            recommendations = [
                "Current approach is effective",
                "Continue monitoring for changes",
                "Consider caching cookies longer to reduce extractions",
            ]
        elif success_rate >= 70:
            severity = "MEDIUM"
            status = "⚠️ MARGINAL"
            message = "Cookie extraction success rate is acceptable but not ideal."
            recommendations = [
                "Increase wait times slightly",
                "Monitor for degradation patterns",
                "Consider reducing extraction frequency",
            ]
        else:
            severity = "HIGH"
            status = "❌ POOR PERFORMANCE"
            message = "Low success rate for cookie extraction."
            recommendations = [
                "Review cookie extraction logic",
                "Check network connectivity",
                "Verify test route has flights",
                "Increase wait times significantly",
            ]
        
        return {
            "severity": severity,
            "status": status,
            "message": message,
            "increasing_failures": increasing_failures,
            "recommendations": recommendations,
        }
    
    def _print_summary(self, summary: Dict):
        """Print formatted summary to console"""
        logger.info("")
        logger.info("📊 TEST RESULTS")
        logger.info("-" * 80)
        
        # Results
        results = summary["results"]
        logger.info(f"Extractions Completed:  {summary['execution']['extractions_completed']}")
        logger.info(f"✅ Successful:         {results['successful']} ({results['success_rate_percent']}%)")
        logger.info(f"❌ Failed:             {results['failed']} ({results['failure_rate_percent']}%)")
        logger.info(f"🚫 Blocked:            {results['blocked']}")
        logger.info("")
        
        # Akamai
        akamai = summary["akamai"]
        logger.info("🛡️ AKAMAI CHALLENGES")
        logger.info("-" * 80)
        logger.info(f"Encountered:           {akamai['challenges_encountered']}")
        logger.info(f"Passed:                {akamai['challenges_passed']}")
        if akamai['pass_rate_percent'] is not None:
            logger.info(f"Pass Rate:             {akamai['pass_rate_percent']}%")
        logger.info("")
        
        # Performance
        perf = summary["performance"]
        logger.info("⚡ PERFORMANCE")
        logger.info("-" * 80)
        logger.info(f"Total Duration:        {summary['execution']['duration_seconds']}s")
        logger.info(f"Avg Extraction Time:   {perf['avg_extraction_time_seconds']}s")
        logger.info(f"Min Extraction Time:   {perf['min_extraction_time_seconds']}s")
        logger.info(f"Max Extraction Time:   {perf['max_extraction_time_seconds']}s")
        logger.info(f"Rate:                  {perf['extractions_per_minute']}/min")
        logger.info("")
        
        # Cookie quality trend
        logger.info("🍪 COOKIE QUALITY TREND")
        logger.info("-" * 80)
        for entry in summary['cookie_quality_over_time'][:5]:  # Show first 5
            status_icon = "✓" if entry['status'] == 'success' else "✗"
            logger.info(f"  #{entry['attempt']}: {status_icon} {entry['status']} "
                       f"({entry['cookies_count']} cookies)")
        if len(summary['cookie_quality_over_time']) > 5:
            logger.info(f"  ... and {len(summary['cookie_quality_over_time']) - 5} more")
        logger.info("")
        
        # Failure trend
        logger.info("📈 FAILURE PATTERN")
        logger.info("-" * 80)
        for trend in summary['failure_trend']:
            logger.info(f"  Extractions {trend['extraction_range']}: "
                       f"{trend['failures']} failures ({trend['failure_rate']:.0f}%)")
        logger.info("")
        
        # Assessment
        assessment = summary["assessment"]
        logger.info("🎯 ASSESSMENT")
        logger.info("-" * 80)
        logger.info(f"Status: {assessment['status']}")
        logger.info(f"Severity: {assessment['severity']}")
        logger.info(f"Message: {assessment['message']}")
        if assessment['increasing_failures']:
            logger.warning("⚠️ WARNING: Failure rate is INCREASING over time!")
        logger.info("")
        logger.info("Recommendations:")
        for i, rec in enumerate(assessment['recommendations'], 1):
            logger.info(f"  {i}. {rec}")
        logger.info("")
        
        # Status breakdown
        logger.info("📊 STATUS BREAKDOWN")
        logger.info("-" * 80)
        for status, count in sorted(summary['status_breakdown'].items()):
            logger.info(f"  {status}: {count}")
        logger.info("=" * 80)
    
    def _save_results(self, summary: Dict):
        """Save detailed results to file"""
        output_dir = Path("./stress_test_results")
        output_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Save summary
        summary_file = output_dir / f"cookie_test_{timestamp}_summary.json"
        summary_file.write_text(json.dumps(summary, indent=2))
        
        # Save detailed results
        detailed_results = [asdict(r) for r in self.results]
        details_file = output_dir / f"cookie_test_{timestamp}_details.json"
        details_file.write_text(json.dumps(detailed_results, indent=2))
        
        logger.info(f"💾 Results saved:")
        logger.info(f"   Summary: {summary_file}")
        logger.info(f"   Details: {details_file}")


async def main():
    """Main entry point"""
    import argparse
    import sys
    
    parser = argparse.ArgumentParser(
        description="Cookie extraction stress test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard test (20 extractions, 5s delay)
  python cookie_stress_test.py
  
  # Aggressive test (50 extractions, 2s delay)
  python cookie_stress_test.py --extractions 50 --delay 2
  
  # Slower test (10 extractions, 30s delay, non-headless)
  python cookie_stress_test.py --extractions 10 --delay 30 --no-headless
  
  # Long wait time test
  python cookie_stress_test.py --wait-time 30 --extractions 15
        """
    )
    
    parser.add_argument(
        "--extractions",
        type=int,
        default=20,
        help="Number of cookie extractions to perform (default: 20)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=5.0,
        help="Seconds between extractions (default: 5.0)"
    )
    parser.add_argument(
        "--wait-time",
        type=int,
        default=15,
        help="Seconds to wait for API during extraction (default: 15)"
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser in visible mode (better evasion)"
    )
    parser.add_argument(
        "--origin",
        type=str,
        default="SRQ",
        help="Test origin airport (default: SRQ)"
    )
    parser.add_argument(
        "--destination",
        type=str,
        default="BFL",
        help="Test destination airport (default: BFL)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    from aa_scraper.logging_config import setup_logging
    log_file = Path("./logs/cookie_stress_test.log")
    setup_logging(verbose=args.verbose, log_file=log_file)
    
    # Initialize test
    test = CookieStressTest(
        total_extractions=args.extractions,
        delay_between=args.delay,
        headless=not args.no_headless,
        wait_time=args.wait_time,
        test_origin=args.origin,
        test_destination=args.destination,
    )
    
    # Run test
    try:
        await test.run()
    except KeyboardInterrupt:
        logger.warning("Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Test failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())