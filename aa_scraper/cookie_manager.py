"""Cookie management with automatic refresh and retry logic"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

from loguru import logger

from .config import (
    BASE_URL,
    COOKIE_MAX_AGE,
    COOKIE_WARNING_AGE,
)
from .exceptions import CookieExpiredError


class CookieManager:
    """
    Manages cookies with automatic refresh, validation, and retry logic.
    Tracks cookie age and automatically extracts when needed.
    Handles rate limiting by clearing old cookies and backing off.
    """

    def __init__(
        self,
        cookie_file: Path,
        test_origin: str = "SRQ",
        test_destination: str = "BFL",
        test_days_ahead: int = 7,
    ):
        """
        Initialize cookie manager.

        Args:
            cookie_file: Path to cookie JSON file
            test_origin: Airport code for cookie validation
            test_destination: Airport code for cookie validation
            test_days_ahead: Days ahead for test date
        """
        self.cookie_file = cookie_file
        self.test_origin = test_origin
        self.test_destination = test_destination
        self.test_days_ahead = test_days_ahead

        self.cookies: Dict[str, str] = {}
        self.headers: Dict[str, str] = {}
        self.referer: str = ""
        self.extract_time: Optional[datetime] = None
        self.lock = asyncio.Lock()
        
        # Retry tracking
        self.consecutive_failures = 0
        self.last_failure_time: Optional[datetime] = None

        logger.info(f"Cookie manager initialized: {cookie_file}")

    def _get_cookie_age(self) -> Optional[float]:
        """Get age of cookies in seconds"""
        if not self.extract_time:
            # Try to get from file modification time
            if self.cookie_file.exists():
                mtime = self.cookie_file.stat().st_mtime
                return datetime.now().timestamp() - mtime
            return None

        return (datetime.now() - self.extract_time).total_seconds()

    def _is_cookie_valid(self) -> bool:
        """Check if cookies are still valid (not expired)"""
        age = self._get_cookie_age()
        if age is None:
            return False

        if age > COOKIE_MAX_AGE:
            logger.warning(f"Cookies expired: {age:.0f}s old (max: {COOKIE_MAX_AGE}s)")
            return False

        if age > COOKIE_WARNING_AGE:
            logger.warning(f"Cookies aging: {age:.0f}s old (warn: {COOKIE_WARNING_AGE}s)")

        # Check critical cookies exist
        critical = ["XSRF-TOKEN", "spa_session_id"]
        missing = [c for c in critical if c not in self.cookies]
        if missing:
            logger.warning(f"Missing critical cookies: {missing}")
            return False

        return True

    def _clear_cookies(self) -> None:
        """Clear all stored cookies and related data"""
        logger.warning("🗑️ Clearing old cookies...")
        self.cookies = {}
        self.headers = {}
        self.referer = ""
        self.extract_time = None
        
        # Remove cookie files
        try:
            if self.cookie_file.exists():
                self.cookie_file.unlink()
            
            headers_file = self.cookie_file.parent / f"{self.cookie_file.stem}_headers.json"
            if headers_file.exists():
                headers_file.unlink()
            
            referer_file = self.cookie_file.parent / f"{self.cookie_file.stem}_referer.txt"
            if referer_file.exists():
                referer_file.unlink()
                
            logger.success("✓ Old cookies cleared")
        except Exception as e:
            logger.warning(f"Error clearing cookie files: {e}")

    def _is_rate_limited(self, error_msg: str) -> bool:
        """Detect if error is due to rate limiting"""
        rate_limit_indicators = [
            "valid api response not received",
            "timeout",
            "no valid pricing",
            "empty slices",
            "api request did not complete",
        ]
        error_lower = error_msg.lower()
        return any(indicator in error_lower for indicator in rate_limit_indicators)

    def _calculate_backoff(self, attempt: int, base_delay: float = 30.0) -> float:
        """Calculate exponential backoff delay"""
        import random
        
        # Exponential backoff: base * 2^attempt with jitter
        delay = base_delay * (2 ** attempt)
        # Add jitter (±25%)
        jitter = random.uniform(0.75, 1.25)
        final_delay = min(delay * jitter, 300.0)  # Cap at 5 minutes
        
        return final_delay

    async def get_cookies(
        self, 
        force_refresh: bool = False, 
        headless: bool = True, 
        wait_time: int = 15,
        max_retries: int = 3,
        base_retry_delay: float = 30.0,
    ) -> Tuple[Dict[str, str], Dict[str, str], str]:
        """
        Get cookies with automatic refresh and retry logic.
        Thread-safe with lock. Handles rate limiting intelligently.

        Args:
            force_refresh: Force cookie extraction even if valid
            headless: Run browser in headless mode
            wait_time: Seconds to wait for API response
            max_retries: Maximum retry attempts for cookie extraction
            base_retry_delay: Base delay in seconds for retry backoff

        Returns:
            Tuple of (cookies, headers, referer)
            
        Raises:
            CookieExpiredError: If cookie extraction fails after all retries
        """
        async with self.lock:
            # Load from file if not in memory
            if not self.cookies and self.cookie_file.exists():
                logger.info("Loading cookies from file...")
                self._load_from_file()

            # Check if refresh needed
            needs_refresh = (
                force_refresh or not self.cookies or not self._is_cookie_valid()
            )

            if not needs_refresh:
                age = self._get_cookie_age()
                logger.info(f"Using cached cookies (age: {age:.0f}s)")
                # Reset failure counter on successful use
                self.consecutive_failures = 0
                return self.cookies, self.headers, self.referer

            # Try to extract with retries
            logger.info("Extracting fresh cookies with retry logic...")
            
            for attempt in range(max_retries):
                try:
                    await self._extract_fresh_cookies(headless, wait_time)
                    
                    # Success! Reset failure tracking
                    self.consecutive_failures = 0
                    self.last_failure_time = None
                    
                    logger.success(f"✅ Cookie extraction successful (attempt {attempt + 1}/{max_retries})")
                    return self.cookies, self.headers, self.referer
                    
                except CookieExpiredError as e:
                    self.consecutive_failures += 1
                    self.last_failure_time = datetime.now()
                    
                    error_msg = str(e)
                    is_last_attempt = (attempt == max_retries - 1)
                    
                    logger.error(f"❌ Cookie extraction failed (attempt {attempt + 1}/{max_retries}): {error_msg}")
                    
                    if is_last_attempt:
                        logger.error(f"🛑 All {max_retries} retry attempts exhausted")
                        raise
                    
                    # Check if we're rate limited
                    if self._is_rate_limited(error_msg):
                        logger.warning(f"⚠️ Rate limiting detected (failure #{self.consecutive_failures})")
                        
                        # Clear old cookies - they might be flagged
                        self._clear_cookies()
                        
                        # Calculate backoff
                        backoff_delay = self._calculate_backoff(attempt, base_retry_delay)
                        
                        logger.warning(
                            f"⏳ Backing off for {backoff_delay:.1f}s before retry "
                            f"(attempt {attempt + 2}/{max_retries})..."
                        )
                        
                        await asyncio.sleep(backoff_delay)
                    else:
                        # Non-rate-limit error, shorter backoff
                        short_delay = 10.0
                        logger.info(f"⏳ Waiting {short_delay}s before retry...")
                        await asyncio.sleep(short_delay)
                
                except Exception as e:
                    self.consecutive_failures += 1
                    logger.error(f"❌ Unexpected error during cookie extraction: {e}")
                    
                    if attempt == max_retries - 1:
                        raise CookieExpiredError(f"Cookie extraction failed after {max_retries} attempts: {e}")
                    
                    # Short delay for unexpected errors
                    await asyncio.sleep(10.0)

            # This shouldn't be reached, but just in case
            raise CookieExpiredError(f"Cookie extraction failed after {max_retries} attempts")

    def _load_from_file(self) -> None:
        """Load cookies and headers from files"""
        try:
            # Load cookies
            if self.cookie_file.exists():
                data = json.loads(self.cookie_file.read_text())
                self.cookies = data
                logger.info(f"Loaded {len(self.cookies)} cookies from {self.cookie_file}")

            # Load headers
            headers_file = self.cookie_file.parent / f"{self.cookie_file.stem}_headers.json"
            if headers_file.exists():
                self.headers = json.loads(headers_file.read_text())
                logger.info(f"Loaded {len(self.headers)} headers")

            # Load referer
            referer_file = self.cookie_file.parent / f"{self.cookie_file.stem}_referer.txt"
            if referer_file.exists():
                self.referer = referer_file.read_text().strip()
                logger.info("Loaded referer")

        except Exception as e:
            logger.error(f"Failed to load cookies from file: {e}")

    async def _extract_fresh_cookies(self, headless: bool, wait_time: int) -> None:
        """Extract cookies using Camoufox browser automation"""
        from camoufox.async_api import AsyncCamoufox
        import urllib.parse

        logger.info(f"🦊 Extracting cookies: {self.test_origin} → {self.test_destination}")

        # Build departure date
        departure_date = (
            datetime.now() + timedelta(days=self.test_days_ahead)
        ).strftime("%Y-%m-%d")

        # Build direct search URL
        slices_data = [
            {
                "orig": self.test_origin,
                "origNearby": False,
                "dest": self.test_destination,
                "destNearby": False,
                "date": departure_date,
            }
        ]

        slices_json = json.dumps(slices_data, separators=(",", ":"))

        search_url = (
            f"{BASE_URL}/booking/search?"
            f"locale=en_US&"
            f"fareType=Lowest&"
            f"pax=1&"
            f"adult=1&"
            f"type=OneWay&"
            f"searchType=Revenue&"
            f"cabin=&"
            f"carriers=ALL&"
            f"travelType=personal&"
            f"slices={urllib.parse.quote(slices_json)}"
        )

        captured_headers = {}
        captured_referer = ""
        captured_cookies = {}
        api_response_data = None
        api_request_completed = False

        try:
            async with AsyncCamoufox(headless=headless) as browser:
                page = await browser.new_page()
                start_time = datetime.now()

                # STEP 1: Go to homepage and accept cookies
                logger.info("Step 1/5: Loading homepage and accepting cookies...")
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

                cookie_accepted = await self._accept_cookie_consent(page)
                if cookie_accepted:
                    logger.success("   ✅ Cookie consent accepted on homepage!")
                    await page.wait_for_timeout(1000)
                else:
                    logger.warning("   ⚠️ Cookie banner not found (may already be accepted)")

                elapsed = (datetime.now() - start_time).total_seconds()
                logger.info(f"   Homepage loaded and consent accepted in {elapsed:.1f}s")

                # STEP 2: Response interception
                async def handle_response(response):
                    nonlocal api_response_data, api_request_completed, captured_headers

                    if "/booking/api/search/itinerary" in response.url:
                        try:
                            status = response.status
                            logger.debug(f"🎯 API response intercepted: HTTP {status}")

                            if status == 200:
                                try:
                                    data = await response.json()

                                    # Validate response
                                    if "slices" not in data:
                                        logger.warning("⚠️ API response missing 'slices' field")
                                        return

                                    slices = data.get("slices", [])
                                    if len(slices) == 0:
                                        logger.warning("⚠️ API response has empty slices array")
                                        return

                                    # Check for valid pricing
                                    has_valid_pricing = False
                                    for slice_data in slices:
                                        pricing = slice_data.get("pricingDetail", [])
                                        if pricing and len(pricing) > 0:
                                            for price_option in pricing:
                                                if price_option.get("productAvailable", False):
                                                    has_valid_pricing = True
                                                    break
                                        if has_valid_pricing:
                                            break

                                    if not has_valid_pricing:
                                        logger.warning("⚠️ API response has no valid pricing data")
                                        return

                                    # SUCCESS!
                                    api_response_data = data
                                    api_request_completed = True

                                    # Capture request headers
                                    request = response.request
                                    raw_headers = dict(request.headers)
                                    captured_headers = self._clean_headers(raw_headers)

                                    logger.success("✅ Valid API response received!")
                                    logger.debug(f"   Slices: {len(slices)}")
                                    logger.debug(f"   Has pricing: {has_valid_pricing}")

                                except json.JSONDecodeError as e:
                                    logger.warning(f"⚠️ API response not valid JSON: {e}")
                            else:
                                logger.warning(f"⚠️ API returned non-200 status: {status}")

                        except Exception as e:
                            logger.warning(f"⚠️ Error processing API response: {e}")

                page.on("response", handle_response)

                # STEP 3: Navigate to search page
                logger.info("Step 2/5: Navigating to search page with validated cookies...")
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

                elapsed = (datetime.now() - start_time).total_seconds()
                logger.info(f"   Search page loaded in {elapsed:.1f}s")

                # STEP 4: Detect and handle Akamai challenge
                current_url = page.url
                page_content = await page.content()

                is_akamai, challenge_type = self._detect_akamai_challenge(
                    current_url, page_content
                )

                if is_akamai:
                    elapsed = (datetime.now() - start_time).total_seconds()
                    logger.warning(f"🛡️ Akamai challenge detected! ({challenge_type})")
                    logger.info("   Waiting for challenge to complete...")

                    try:
                        await page.wait_for_function(
                            """
                            () => {
                                const url = window.location.href;
                                const content = document.body.innerHTML;
                                return !url.includes('akamai') && 
                                    !url.includes('challenge') &&
                                    !content.includes('sec_chlge_form') &&
                                    (url.includes('choose-flights') || 
                                        url.includes('find-flights') || 
                                        url.includes('booking'));
                            }
                            """,
                            timeout=90000,
                        )

                        total_elapsed = (datetime.now() - start_time).total_seconds()
                        logger.success(f"✓ Akamai challenge passed! ({total_elapsed:.1f}s)")

                    except Exception as e:
                        logger.error(f"❌ Akamai challenge timeout: {e}")
                        await page.screenshot(path="error_akamai_timeout.png")
                        raise CookieExpiredError("Akamai challenge failed")
                else:
                    elapsed = (datetime.now() - start_time).total_seconds()
                    logger.success(f"✓ No Akamai challenge ({elapsed:.1f}s)")

                # STEP 5: Wait for API request with valid response
                logger.info("Step 4/5: Waiting for VALID API response...")

                max_wait = wait_time
                elapsed = 0
                check_interval = 1

                while elapsed < max_wait and not api_request_completed:
                    await page.wait_for_timeout(check_interval * 1000)
                    elapsed += check_interval

                    if elapsed % 5 == 0:
                        logger.debug(f"   Waiting... ({elapsed}/{max_wait}s)")

                if not api_request_completed:
                    logger.warning(f"⚠️ Valid API response not received after {max_wait}s")

                    # Try scrolling
                    logger.info("   Attempting scroll to trigger API...")
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(5000)

                    if not api_request_completed:
                        logger.error("❌ Still no valid API response")
                        await page.screenshot(path="error_no_valid_api.png")
                        raise CookieExpiredError("Valid API response not received")

                # STEP 6: Extract cookies
                logger.info("Step 5/5: Extracting validated cookies...")

                final_url = page.url
                captured_referer = final_url

                raw_cookies = await page.context.cookies()
                for cookie in raw_cookies:
                    captured_cookies[cookie["name"]] = cookie["value"]

                logger.debug(f"✓ Extracted {len(captured_cookies)} cookies")

                # Validate cookies
                self._validate_extracted_cookies(captured_cookies)

            # Final validation
            if not api_request_completed or not api_response_data:
                raise CookieExpiredError("API request did not complete successfully")

            slices = api_response_data.get("slices", [])
            if not slices:
                raise CookieExpiredError("Invalid API response - no flight slices")

            # SUCCESS - Save everything
            self.cookies = captured_cookies
            self.headers = captured_headers
            self.referer = captured_referer
            self.extract_time = datetime.now()

            self._save_to_file()

            total_time = (datetime.now() - start_time).total_seconds()

            logger.success(f"🎉 Cookie extraction complete in {total_time:.1f}s:")
            logger.info(f"   • Cookies: {len(captured_cookies)}")
            logger.info(f"   • Headers: {len(captured_headers)}")
            logger.info("   • API validated: ✓")
            logger.info(f"   • Slices: {len(slices)}")

        except Exception as e:
            logger.error(f"Cookie extraction failed: {e}")
            raise CookieExpiredError(f"Failed to extract cookies: {e}")

    async def _accept_cookie_consent(self, page) -> bool:
        """Accept cookie consent banner"""
        selectors = [
            "#accept-recommended-btn-handler",
            "#onetrust-accept-btn-handler",
            'button:has-text("Accept")',
            'button:has-text("Accept all")',
            'button:has-text("Aceptar")',
        ]

        for selector in selectors:
            try:
                accept_btn = await page.wait_for_selector(
                    selector, timeout=5000, state="visible"
                )

                if accept_btn:
                    logger.debug(f"   Found cookie button: {selector}")
                    await accept_btn.click()
                    logger.debug("   ✓ Cookie button clicked")
                    await page.wait_for_timeout(1500)
                    return True

            except Exception:
                continue

        return False

    def _detect_akamai_challenge(
        self, url: str, page_content: str
    ) -> Tuple[bool, str]:
        """Detect if page is showing Akamai bot challenge"""
        # Check URL for Akamai paths
        akamai_url_patterns = {
            "akamai_path": "/ZetFNOmfUz0qb36s_",
            "akamai_path2": "/booking/api/akamai",
            "challenge_resubmit": "akamai-challenge-resubmit",
        }

        url_lower = url.lower()
        for challenge_type, pattern in akamai_url_patterns.items():
            if pattern.lower() in url_lower:
                return True, challenge_type

        # Check page content for challenge markers
        akamai_content_markers = {
            "challenge_iframe": 'title="Challenge Content"',
            "challenge_form": "sec_chlge_form",
            "challenge_script": "cp_clge_done",
            "crypto_provider": 'provider="crypto"',
            "sec_container": 'class="sec-container"',
        }

        for challenge_type, marker in akamai_content_markers.items():
            if marker in page_content:
                return True, challenge_type

        return False, "none"

    def _validate_extracted_cookies(self, cookies: Dict[str, str]) -> None:
        """Validate that we captured essential cookies"""
        # Critical cookies (must have)
        critical_cookies = ["XSRF-TOKEN", "spa_session_id"]

        # Important cookies (should have)
        important_cookies = ["JSESSIONID", "_abck", "bm_sv"]

        # Bot defense cookies (good to have)
        bot_cookies = ["bm_sz", "ak_bmsc", "bm_s", "sec_cpt"]

        # Check critical
        found_critical = [c for c in critical_cookies if c in cookies]
        missing_critical = [c for c in critical_cookies if c not in cookies]

        if found_critical:
            logger.debug(f"  ✓ Critical: {', '.join(found_critical)}")
        if missing_critical:
            logger.error(f"  ❌ Missing critical: {', '.join(missing_critical)}")
            raise CookieExpiredError(f"Missing critical cookies: {missing_critical}")

        # Check important
        found_important = [c for c in important_cookies if c in cookies]
        if found_important:
            logger.debug(f"  ✓ Important: {', '.join(found_important)}")
        else:
            logger.warning("  ⚠️ No important cookies (may cause issues)")

        # Check bot defense
        found_bot = [c for c in bot_cookies if c in cookies]
        if found_bot:
            logger.debug(f"  ✓ Bot-defense: {', '.join(found_bot)}")
        else:
            logger.warning("  ⚠️ No bot-defense cookies")

    def _clean_headers(self, raw_headers: Dict[str, str]) -> Dict[str, str]:
        """Clean headers for httpx use"""
        SKIP = {"host", "content-length", "connection", "cookie", "accept-encoding"}
        return {
            k: v
            for k, v in raw_headers.items()
            if not k.lower().startswith(":") and k.lower() not in SKIP
        }

    def _save_to_file(self) -> None:
        """Save cookies, headers, and referer to files"""
        try:
            self.cookie_file.parent.mkdir(parents=True, exist_ok=True)

            # Save cookies
            self.cookie_file.write_text(json.dumps(self.cookies, indent=2))

            # Save headers
            headers_file = (
                self.cookie_file.parent / f"{self.cookie_file.stem}_headers.json"
            )
            headers_file.write_text(json.dumps(self.headers, indent=2))

            # Save referer
            referer_file = self.cookie_file.parent / f"{self.cookie_file.stem}_referer.txt"
            referer_file.write_text(self.referer)

            logger.info(f"💾 Saved cookies to: {self.cookie_file}")

        except Exception as e:
            logger.error(f"Failed to save cookies: {e}")