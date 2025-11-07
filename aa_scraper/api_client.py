"""API client for American Airlines flight search"""

from datetime import datetime
from typing import Any, Dict, Optional

from curl_cffi.requests import AsyncSession
from curl_cffi import CurlError
import json
import time
from loguru import logger

from .circuit_breaker import CircuitBreaker
from .config import API_ENDPOINT, BASE_URL
from .cookie_manager import CookieManager
from .exceptions import (
    CircuitOpenError,
    CookieExpiredError,
    IPBlockedError,
    RateLimitError,
)
from .rate_limiter import AdaptiveRateLimiter
from .retry import retry_with_backoff


class AAFlightClient:
    """
    Enhanced AA flight search client with:
    - curl_cffi for Firefox fingerprint matching Camoufox
    - Automatic cookie refresh on 403 errors
    - Circuit breaker pattern
    - Exponential backoff retry
    - Request health checks
    - Fresh HTTP client per request (prevents bot‑detection)
    """

    def __init__(
        self,
        cookie_manager: CookieManager,
        rate_limiter: AdaptiveRateLimiter,
        timeout: float = 30.0,  # browser‑like timeout
    ):
        """
        Initialize API client.

        Args:
            cookie_manager: Cookie manager instance
            rate_limiter: Rate limiter instance
            timeout: Request timeout in seconds
        """
        self.cookie_manager = cookie_manager
        self.rate_limiter = rate_limiter
        self.timeout = timeout
        self.circuit_breaker = CircuitBreaker(name="aa_api")
        self.session_start = datetime.now()

        # Firefox 135 matches Camoufox best (latest stable Firefox fingerprint)
        self.impersonate = "firefox135"

        logger.info(f"Flight client initialized with curl_cffi (impersonate: {self.impersonate})")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def _build_headers(
        self,
        cookies: Dict[str, str],
        captured_headers: Dict[str, str],
        referer: str,
    ) -> Dict[str, str]:
        """Build request headers with proper ordering"""
        HEADER_ORDER = [
            "user-agent",
            "accept",
            "accept-language",
            "content-type",
            "referer",
            "x-xsrf-token",
            "x-cid",
            "origin",
            "sec-fetch-dest",
            "sec-fetch-mode",
            "sec-fetch-site",
            "priority",
            "te",
        ]

        if captured_headers:
            captured_lower = {k.lower(): (k, v) for k, v in captured_headers.items()}
            headers = {}

            for header_name in HEADER_ORDER:
                if header_name in captured_lower:
                    original_key, value = captured_lower[header_name]
                    headers[original_key] = value

            # Add any remaining headers that were not in the ordered list
            for key, value in captured_headers.items():
                if key.lower() not in [h.lower() for h in headers.keys()]:
                    headers[key] = value

            # Ensure the referer is what we want
            if referer:
                for key in list(headers.keys()):
                    if key.lower() == "referer":
                        headers[key] = referer
                        break
                else:
                    headers["Referer"] = referer
        else:
            # Minimal fallback headers (curl_cffi will add browser headers automatically)
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/json",
                "Origin": BASE_URL,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
            if referer:
                headers["Referer"] = referer

        # Critical headers that must be present
        headers_lower = {k.lower(): k for k in headers.keys()}
        if "x-xsrf-token" not in headers_lower and "XSRF-TOKEN" in cookies:
            headers["X-XSRF-TOKEN"] = cookies["XSRF-TOKEN"]
        if "x-cid" not in headers_lower and "spa_session_id" in cookies:
            headers["X-CID"] = cookies["spa_session_id"]

        return headers

    def _build_request_payload(
        self,
        origin: str,
        destination: str,
        date: str,
        passengers: int,
        search_type: str,
    ) -> Dict[str, Any]:
        """Build API request payload"""
        payload = {
            "metadata": {
                "selectedProducts": [],
                "tripType": "OneWay",
                "udo": {},
            },
            "passengers": [{"type": "adult", "count": passengers}],
            "requestHeader": {"clientId": "AAcom"},
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
                    "originNearbyAirports": False,
                }
            ],
            "tripOptions": {
                "corporateBooking": False,
                "fareType": "Lowest",
                "locale": "en_US",
                "pointOfSale": "",
                "searchType": search_type,
            },
            "loyaltyInfo": None,
            "version": "cfr" if search_type == "Revenue" else "",
            "queryParams": {
                "sliceIndex": 0,
                "sessionId": "",
                "solutionSet": "",
                "solutionId": "",
                "sort": "CARRIER",
            },
        }

        if search_type == "Revenue":
            payload["metadata"]["udo"]["search_method"] = "Lowest"

        return payload

    async def _make_request(
        self,
        origin: str,
        destination: str,
        date: str,
        passengers: int,
        search_type: str,
    ) -> Dict[str, Any]:
        """Make a single API request using curl_cffi (fresh session per call)"""
        # Get fresh cookies / headers / referer
        cookies, captured_headers, referer = await self.cookie_manager.get_cookies()
        headers = self._build_headers(cookies, captured_headers, referer)

        # Build payload
        payload = self._build_request_payload(
            origin, destination, date, passengers, search_type
        )

        # Log request meta‑data
        request_id = f"{origin}-{destination}-{date}-{search_type}"
        logger.info(f"🔍 [{request_id}] Starting {search_type} search with curl_cffi (Firefox impersonation)")
        logger.debug(f"   Route: {origin} → {destination}")
        logger.debug(f"   Date: {date}")
        logger.debug(f"   Passengers: {passengers}")
        logger.debug(f"   Impersonate: {self.impersonate}")

        # Acquire rate limit token
        await self.rate_limiter.acquire()

        # Create fresh AsyncSession with Firefox impersonation
        async with AsyncSession(impersonate=self.impersonate) as session:
            start_time = time.time()
            response_size = 0
            try:
                response = await session.post(
                    API_ENDPOINT,
                    json=payload,
                    headers=headers,
                    cookies=cookies,
                    timeout=self.timeout,
                )
                request_duration = time.time() - start_time
                
                # Track response size
                response_size = len(response.content) if response.content else 0

                logger.debug(f"   ← Response {response.status_code} ({request_duration:.2f}s)")
                logger.debug(f"   ← Content-Type: {response.headers.get('content-type', 'unknown')}")

            except TimeoutError as e:
                logger.error(f"❌ [{request_id}] REQUEST TIMEOUT")
                logger.error(f"   Error: {e}")
                raise
            except CurlError as e:
                logger.error(f"❌ [{request_id}] CURL ERROR")
                logger.error(f"   Error: {e}")
                raise
            except Exception as e:
                logger.error(f"❌ [{request_id}] HTTP ERROR")
                logger.error(f"   Error: {e}")
                raise

        # Handle special status codes / HTML blocks
        content_type = response.headers.get("content-type", "").lower()

        if "text/html" in content_type or response.status_code in {403, 451}:
            logger.warning(f"⚠️ [{request_id}] Unexpected HTML response (possible block)")
            logger.warning(f"   Status: {response.status_code}")
            logger.warning(f"   Content-Type: {content_type}")

            html = response.text
            if self._detect_permission_denied_in_response(html):
                logger.error(f"🚫 [{request_id}] IP BLOCKED - Permission Denied")
                raise IPBlockedError(
                    "Server blocked IP with 'Permission Denied'. "
                    "Wait ~40 minutes before retrying."
                )
            else:
                raise Exception(
                    f"Unexpected HTML response (status {response.status_code})"
                )

        if response.status_code == 403:
            logger.error(f"❌ [{request_id}] 403 FORBIDDEN – likely cookie / bot issue")
            raise CookieExpiredError("403 Forbidden – cookies may be invalid")

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "60"))
            logger.error(f"⏰ [{request_id}] 429 RATE LIMITED – retry after {retry_after}s")
            await self.rate_limiter.backoff(retry_after)
            raise RateLimitError(f"Rate limited, retry after {retry_after}s")

        if response.status_code >= 400:
            logger.error(f"❌ [{request_id}] HTTP {response.status_code} ERROR")
            raise Exception(f"HTTP {response.status_code} error")

        # Parse JSON
        try:
            data = response.json()
        except json.JSONDecodeError as e:
            logger.error(f"❌ [{request_id}] INVALID JSON RESPONSE")
            logger.error(f"   Error: {e}")
            raise

        # Verify expected structure
        if "slices" not in data:
            logger.error(f"❌ [{request_id}] MISSING 'slices' in API response")
            raise ValueError("API response missing 'slices' field")

        slices = data.get("slices", [])
        logger.success(
            f"✅ [{request_id}] {search_type} search successful ({request_duration:.2f}s)"
        )
        logger.info(f"   Slices Received: {len(slices)}")
        if slices:
            first = slices[0]
            logger.debug(
                f"   First Slice – stops: {first.get('stops')}, "
                f"duration: {first.get('durationInMinutes')}min, "
                f"segments: {len(first.get('segments', []))}"
            )

        # Tell rate limiter we finished successfully
        await self.rate_limiter.recover()

        metrics = {
            'response_time': request_duration,
            'response_bytes': response_size,
            'status_code': response.status_code,
        }
        
        return data, metrics

    def _detect_permission_denied_in_response(self, response_text: str) -> bool:
        """
        Detect if API response contains Access Denied / Permission Denied page.
        Matches the actual Akamai Access Denied HTML structure.
        """
        text_lower = response_text.lower()

        # Primary patterns (Akamai Access Denied – actual page structure)
        akamai_patterns = [
            "<title>access denied</title>",
            "<h1>access denied</h1>",
            "you don't have permission to access",
            "errors.edgesuite.net",
        ]
        akamai_matches = sum(1 for pat in akamai_patterns if pat in text_lower)
        if akamai_matches >= 2:
            return True

        # Secondary patterns (other block types)
        other_patterns = [
            "permission denied",
            "your ip has been blocked",
            "ip address blocked",
            "temporarily blocked",
            "<title>forbidden</title>",
            "<title>403",
        ]

        # Reference number sometimes appears on Akamai blocks
        has_reference = "reference" in text_lower and (
            "reference&#32;&#35;" in text_lower or "reference #" in text_lower
        )

        return any(p in text_lower for p in other_patterns) or has_reference

    async def search_flights(
        self,
        origin: str,
        destination: str,
        date: str,
        passengers: int,
        search_type: str = "Award",
    ) -> Optional[Dict[str, Any]]:
        """
        Search for flights with automatic recovery.
        Uses curl_cffi with Firefox impersonation matching Camoufox.
        Auto‑refreshes cookies on 403 errors.
        """

        async def attempt_search():
            """Single search attempt"""
            return await self._make_request(
                origin,
                destination,
                date,
                passengers,
                search_type,
            )

        async def on_retry_callback(attempt: int, error: Exception):
            """Handle retry attempts (refresh cookies, back‑off, etc.)"""
            from .retry import classify_error
            from .models import ErrorType

            error_type = classify_error(error)

            if error_type == ErrorType.AUTH_FAILURE:
                logger.warning("Auth failure detected – refreshing cookies...")
                try:
                    await self.cookie_manager.get_cookies(force_refresh=True, headless=True)
                    logger.success("✓ Cookies refreshed")
                except Exception as e:
                    logger.error(f"Failed to refresh cookies: {e}")

            elif error_type == ErrorType.RATE_LIMIT:
                logger.warning("Rate limit detected – increasing backoff...")
                await self.rate_limiter.backoff(30)

        try:
            result = await self.circuit_breaker.call(
                retry_with_backoff,
                attempt_search,
                on_retry=on_retry_callback,
            )
            if result:
                logger.success(f"✅ {search_type} search successful")
            return result

        except CircuitOpenError as e:
            logger.error(f"Circuit breaker open: {e}")
            return None
        except Exception as e:
            logger.error(f"Search failed after all retries: {e}")
            return None