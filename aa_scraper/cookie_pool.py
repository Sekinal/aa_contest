"""Multi-browser cookie pool for parallel scraping with different cookies"""

import asyncio
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

from loguru import logger

from .cookie_manager import CookieManager
from .config import DEFAULT_TEST_ORIGIN, DEFAULT_TEST_DESTINATION, DEFAULT_TEST_DAYS_AHEAD


class CookiePool:
    """
    Manages multiple browser instances with independent cookies.
    Each browser can handle max_concurrent requests in parallel.
    
    This allows bypassing Akamai rate limits by appearing as different users.
    
    Example:
        - 5 browsers × 5 concurrent = 25 total concurrent requests
        - Each browser has its own cookies (appears as different user)
        - Akamai sees 5 separate users making 5 requests each (safe)
    """
    
    def __init__(
        self,
        num_browsers: int,
        base_cookie_dir: Path,
        max_concurrent_per_browser: int = 5,
        test_origin: str = DEFAULT_TEST_ORIGIN,
        test_destination: str = DEFAULT_TEST_DESTINATION,
        test_days_ahead: int = DEFAULT_TEST_DAYS_AHEAD,
    ):
        """
        Initialize cookie pool with multiple browsers.
        
        Args:
            num_browsers: Number of browser instances (cookie sets)
            base_cookie_dir: Base directory for cookie files
            max_concurrent_per_browser: Max concurrent requests per browser (default: 5)
            test_origin: Origin for cookie validation
            test_destination: Destination for cookie validation
            test_days_ahead: Days ahead for test date
        """
        self.num_browsers = num_browsers
        self.base_cookie_dir = base_cookie_dir
        self.max_concurrent_per_browser = max_concurrent_per_browser
        
        base_cookie_dir.mkdir(parents=True, exist_ok=True)
        
        # Create browser instances
        self.browsers: List[Dict] = []
        
        for i in range(num_browsers):
            # Each browser gets its own cookie file
            cookie_file = base_cookie_dir / f"aa_cookies_browser_{i}.json"
            
            cookie_manager = CookieManager(
                cookie_file=cookie_file,
                test_origin=test_origin,
                test_destination=test_destination,
                test_days_ahead=test_days_ahead,
            )
            
            # Each browser has its own concurrency semaphore
            semaphore = asyncio.Semaphore(max_concurrent_per_browser)
            
            self.browsers.append({
                'id': i,
                'cookie_file': cookie_file,
                'manager': cookie_manager,
                'semaphore': semaphore,
                'request_count': 0,  # Track usage for stats
            })
        
        logger.info(f"🍪 Cookie pool initialized:")
        logger.info(f"   Browsers: {num_browsers}")
        logger.info(f"   Max concurrent per browser: {max_concurrent_per_browser}")
        logger.info(f"   Total max concurrent: {num_browsers * max_concurrent_per_browser}")
        logger.info(f"   Cookie directory: {base_cookie_dir}")
    
    def get_browser(self, task_id: int) -> Dict:
        """
        Get a browser for a task using round-robin assignment.
        
        Args:
            task_id: Unique task identifier
        
        Returns:
            Browser dict with 'id', 'manager', 'semaphore'
        """
        browser_idx = task_id % self.num_browsers
        browser = self.browsers[browser_idx]
        browser['request_count'] += 1
        return browser
    
    async def initialize_all_cookies(
        self,
        force_refresh: bool = False,
        headless: bool = True,
        wait_time: int = 15,
    ) -> None:
        """
        Initialize cookies for all browsers.
        Extracts cookies if they're old enough or if force_refresh is True.
        
        Args:
            force_refresh: Force fresh extraction for all browsers
            headless: Run browsers in headless mode
            wait_time: Wait time for API response during extraction
        """
        logger.info("")
        logger.info("=" * 80)
        logger.info(f"🔄 INITIALIZING {self.num_browsers} BROWSER COOKIE SETS")
        logger.info("=" * 80)
        
        async def init_browser(browser: Dict):
            """Initialize a single browser's cookies"""
            browser_id = browser['id']
            cookie_manager = browser['manager']
            
            try:
                logger.info(f"🍪 Browser #{browser_id}: Checking cookies...")
                
                # Get cookies (will auto-extract if needed)
                cookies, headers, referer = await cookie_manager.get_cookies(
                    force_refresh=force_refresh,
                    headless=headless,
                    wait_time=wait_time,
                )
                
                logger.success(f"✅ Browser #{browser_id}: Ready ({len(cookies)} cookies)")
                return True
                
            except Exception as e:
                logger.error(f"❌ Browser #{browser_id}: Failed to initialize: {e}")
                return False
        
        # Initialize all browsers concurrently
        logger.info(f"⚡ Initializing all {self.num_browsers} browsers in parallel...")
        start_time = datetime.now()
        
        results = await asyncio.gather(
            *[init_browser(browser) for browser in self.browsers],
            return_exceptions=True
        )
        
        duration = (datetime.now() - start_time).total_seconds()
        
        # Check results
        successful = sum(1 for r in results if r is True)
        failed = self.num_browsers - successful
        
        logger.info("")
        logger.info("=" * 80)
        if failed == 0:
            logger.success(f"✅ All {self.num_browsers} browsers initialized successfully in {duration:.1f}s")
        else:
            logger.warning(f"⚠️ {successful}/{self.num_browsers} browsers initialized ({failed} failed)")
        logger.info("=" * 80)
        logger.info("")
        
        if failed == self.num_browsers:
            raise Exception("All browser cookie initialization failed!")
    
    def get_stats(self) -> Dict:
        """Get usage statistics for all browsers"""
        return {
            'num_browsers': self.num_browsers,
            'max_concurrent_per_browser': self.max_concurrent_per_browser,
            'total_max_concurrent': self.num_browsers * self.max_concurrent_per_browser,
            'browsers': [
                {
                    'id': b['id'],
                    'cookie_file': str(b['cookie_file']),
                    'request_count': b['request_count'],
                }
                for b in self.browsers
            ]
        }
    
    def print_stats(self) -> None:
        """Print usage statistics"""
        logger.info("")
        logger.info("=" * 80)
        logger.info("📊 COOKIE POOL USAGE STATISTICS")
        logger.info("=" * 80)
        
        total_requests = sum(b['request_count'] for b in self.browsers)
        
        logger.info(f"Total Browsers:        {self.num_browsers}")
        logger.info(f"Total Requests:        {total_requests}")
        logger.info(f"Avg per Browser:       {total_requests / self.num_browsers:.1f}")
        logger.info("")
        logger.info("Per-Browser Breakdown:")
        
        for browser in self.browsers:
            logger.info(f"  Browser #{browser['id']}: {browser['request_count']} requests")
        
        logger.info("=" * 80)