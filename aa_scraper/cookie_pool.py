"""Multi-browser cookie pool with optional proxy support"""

import asyncio
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

from loguru import logger

from .cookie_manager import CookieManager
from .proxy_pool import ProxyPool, ProxyConfig
from .config import DEFAULT_TEST_ORIGIN, DEFAULT_TEST_DESTINATION, DEFAULT_TEST_DAYS_AHEAD


class CookiePool:
    """
    Manages multiple browser instances with independent cookies.
    Optionally uses proxy pool for IP rotation.
    
    Without proxies: Creates num_browsers browsers with shared IP
    With proxies: Assigns browsers to proxies (up to 3 per proxy)
    """
    
    def __init__(
        self,
        num_browsers: int,
        base_cookie_dir: Path,
        max_concurrent_per_browser: int = 5,
        test_origin: str = DEFAULT_TEST_ORIGIN,
        test_destination: str = DEFAULT_TEST_DESTINATION,
        test_days_ahead: int = DEFAULT_TEST_DAYS_AHEAD,
        proxy_pool: Optional[ProxyPool] = None,  # 🆕 NEW
    ):
        """
        Initialize cookie pool with optional proxy support.
        
        Args:
            num_browsers: Number of browser instances
            base_cookie_dir: Base directory for cookie files
            max_concurrent_per_browser: Max concurrent per browser
            test_origin: Origin for cookie validation
            test_destination: Destination for cookie validation
            test_days_ahead: Days ahead for test date
            proxy_pool: Optional proxy pool for IP rotation
        """
        self.num_browsers = num_browsers
        self.base_cookie_dir = base_cookie_dir
        self.max_concurrent_per_browser = max_concurrent_per_browser
        self.proxy_pool = proxy_pool  # 🆕 NEW
        
        base_cookie_dir.mkdir(parents=True, exist_ok=True)
        
        # Create browser instances
        self.browsers: List[Dict] = []
        
        mode = "WITH PROXIES" if proxy_pool else "WITHOUT PROXIES (Shared IP)"
        
        logger.info(f"🍪 Initializing cookie pool - {mode}")
        
        for i in range(num_browsers):
            # Each browser gets its own cookie file
            cookie_file = base_cookie_dir / f"aa_cookies_browser_{i}.json"
            
            # 🆕 Assign proxy if pool available
            proxy = None
            if proxy_pool:
                # This is a synchronous call, but we'll do async assignment during init
                proxy = None  # Will be assigned in initialize_all_cookies
            
            cookie_manager = CookieManager(
                cookie_file=cookie_file,
                test_origin=test_origin,
                test_destination=test_destination,
                test_days_ahead=test_days_ahead,
                proxy=proxy,  # 🆕 NEW
            )
            
            # Each browser has its own concurrency semaphore
            semaphore = asyncio.Semaphore(max_concurrent_per_browser)
            
            self.browsers.append({
                'id': i,
                'cookie_file': cookie_file,
                'manager': cookie_manager,
                'semaphore': semaphore,
                'request_count': 0,
                'proxy': proxy,  # 🆕 Track assigned proxy
            })
        
        logger.info(f"   Browsers: {num_browsers}")
        logger.info(f"   Max concurrent per browser: {max_concurrent_per_browser}")
        logger.info(f"   Total max concurrent: {num_browsers * max_concurrent_per_browser}")
        if proxy_pool:
            logger.info(f"   Proxy pool: {len(proxy_pool.proxies)} proxies available")
        logger.info(f"   Cookie directory: {base_cookie_dir}")
    
    def get_browser(self, task_id: int) -> Dict:
        """
        Get a browser for a task using round-robin assignment.
        
        Args:
            task_id: Unique task identifier
        
        Returns:
            Browser dict with 'id', 'manager', 'semaphore', 'proxy'
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
        Initialize cookies for all browsers with proxy assignment.
        
        Args:
            force_refresh: Force fresh extraction for all browsers
            headless: Run browsers in headless mode
            wait_time: Wait time for API response during extraction
        """
        logger.info("")
        logger.info("=" * 80)
        
        if self.proxy_pool:
            logger.info(f"🔄 INITIALIZING {self.num_browsers} BROWSERS WITH PROXY ROTATION")
        else:
            logger.info(f"🔄 INITIALIZING {self.num_browsers} BROWSERS (Shared IP)")
        
        logger.info("=" * 80)
        
        async def init_browser(browser: Dict):
            """Initialize a single browser's cookies with proxy assignment"""
            browser_id = browser['id']
            cookie_manager = browser['manager']
            
            try:
                # 🆕 Assign proxy if pool available
                if self.proxy_pool:
                    proxy = await self.proxy_pool.get_available_proxy(browser_id)
                    
                    if proxy is None:
                        logger.error(f"❌ Browser #{browser_id}: No available proxies!")
                        return False
                    
                    # Update cookie manager with assigned proxy
                    cookie_manager.proxy = proxy
                    browser['proxy'] = proxy
                    
                    logger.info(
                        f"🌐 Browser #{browser_id}: Assigned proxy {proxy.host}:{proxy.port}"
                    )
                
                logger.info(f"🍪 Browser #{browser_id}: Checking cookies...")
                
                # Get cookies (will auto-extract if needed)
                cookies, headers, referer = await cookie_manager.get_cookies(
                    force_refresh=force_refresh,
                    headless=headless,
                    wait_time=wait_time,
                )
                
                # 🆕 Mark proxy success if using proxies
                if self.proxy_pool and browser['proxy']:
                    await self.proxy_pool.mark_proxy_success(browser['proxy'])
                
                logger.success(f"✅ Browser #{browser_id}: Ready ({len(cookies)} cookies)")
                return True
                
            except Exception as e:
                logger.error(f"❌ Browser #{browser_id}: Failed to initialize: {e}")
                
                # 🆕 Handle IP blocking
                from .exceptions import IPBlockedError
                
                if isinstance(e, IPBlockedError) and self.proxy_pool and browser['proxy']:
                    logger.error(f"   Browser #{browser_id}: Proxy got IP blocked!")
                    await self.proxy_pool.mark_proxy_blocked(browser['proxy'])
                    browser['proxy'] = None
                    cookie_manager.proxy = None
                elif self.proxy_pool and browser['proxy']:
                    await self.proxy_pool.mark_proxy_failure(browser['proxy'])
                
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
        
        if self.proxy_pool:
            logger.info("")
            self.proxy_pool.print_stats()
        
        logger.info("=" * 80)
        logger.info("")
        
        if successful == 0:
            raise Exception("All browser cookie initialization failed!")
    
    def get_stats(self) -> Dict:
        """Get usage statistics for all browsers and proxies"""
        stats = {
            'num_browsers': self.num_browsers,
            'max_concurrent_per_browser': self.max_concurrent_per_browser,
            'total_max_concurrent': self.num_browsers * self.max_concurrent_per_browser,
            'browsers': [
                {
                    'id': b['id'],
                    'cookie_file': str(b['cookie_file']),
                    'request_count': b['request_count'],
                    'proxy': f"{b['proxy'].host}:{b['proxy'].port}" if b['proxy'] else None,
                }
                for b in self.browsers
            ]
        }
        
        if self.proxy_pool:
            stats['proxy_pool'] = self.proxy_pool.get_stats()
        
        return stats
    
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
            proxy_info = f" via {browser['proxy'].host}:{browser['proxy'].port}" if browser['proxy'] else " (no proxy)"
            logger.info(f"  Browser #{browser['id']}: {browser['request_count']} requests{proxy_info}")
        
        logger.info("=" * 80)
        
        # Print proxy stats if available
        if self.proxy_pool:
            logger.info("")
            self.proxy_pool.print_stats()