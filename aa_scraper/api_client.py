"""API client for American Airlines flight search"""

from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from .circuit_breaker import CircuitBreaker
from .config import API_ENDPOINT, BASE_URL
from .cookie_manager import CookieManager
from .exceptions import CircuitOpenError, CookieExpiredError, RateLimitError
from .rate_limiter import AdaptiveRateLimiter
from .retry import retry_with_backoff


class AAFlightClient:
    """
    Enhanced AA flight search client with:
    - Automatic cookie refresh on 403 errors
    - Circuit breaker pattern
    - Exponential backoff retry
    - Request health checks
    - Persistent HTTP client connections
    """

    def __init__(
        self,
        cookie_manager: CookieManager,
        rate_limiter: AdaptiveRateLimiter,
        timeout: float = 10.0,
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
        
        # Create persistent HTTP clients (one for HTTP/2, one for HTTP/1.1)
        limits = httpx.Limits(
            max_keepalive_connections=20,
            max_connections=50,
            keepalive_expiry=60.0,
        )
        
        self._client_http2 = httpx.AsyncClient(
            timeout=self.timeout,
            limits=limits,
            http2=True,
            follow_redirects=True,
        )
        
        self._client_http1 = httpx.AsyncClient(
            timeout=self.timeout,
            limits=limits,
            http2=False,
            follow_redirects=True,
        )

        logger.info("Flight client initialized with auto-recovery and persistent connections")

    async def close(self):
        """Close HTTP clients and cleanup resources"""
        await self._client_http2.aclose()
        await self._client_http1.aclose()
        logger.info("Flight client closed")

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()

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

            # Add remaining headers
            for key, value in captured_headers.items():
                if key.lower() not in [h.lower() for h in headers.keys()]:
                    headers[key] = value

            # Override referer
            if referer:
                for key in headers.keys():
                    if key.lower() == "referer":
                        headers[key] = referer
                        break
                else:
                    headers["Referer"] = referer
        else:
            # Fallback headers
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US",
                "Content-Type": "application/json",
                "Origin": BASE_URL,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
            if referer:
                headers["Referer"] = referer

        # Ensure critical headers
        headers_lower = {k.lower(): k for k in headers.keys()}
        if "x-xsrf-token" not in headers_lower and "XSRF-TOKEN" in cookies:
            headers["X-XSRF-TOKEN"] = cookies["XSRF-TOKEN"]
        if "x-cid" not in headers_lower and "spa_session_id" in cookies:
            headers["X-CID"] = cookies["spa_session_id"]
        if "user-agent" not in headers_lower:
            headers[
                "User-Agent"
            ] = "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0"

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
        http_version: str = "HTTP/2",
    ) -> Dict[str, Any]:
        """Make a single API request using persistent client"""
        # Get fresh cookies
        cookies, captured_headers, referer = await self.cookie_manager.get_cookies()
        headers = self._build_headers(cookies, captured_headers, referer)
        payload = self._build_request_payload(
            origin, destination, date, passengers, search_type
        )

        # Acquire rate limit token
        await self.rate_limiter.acquire()

        # Select appropriate persistent client
        client = self._client_http2 if http_version == "HTTP/2" else self._client_http1

        logger.info(
            f"🔍 {search_type}: {origin} → {destination} on {date} (via {http_version})"
        )

        # Make request with persistent client (pass cookies and headers per-request)
        response = await client.post(
            API_ENDPOINT,
            json=payload,
            headers=headers,
            cookies=cookies,
        )

        logger.debug(f"Response: {response.status_code}")

        # Handle specific status codes
        if response.status_code == 403:
            logger.warning("Got 403 - bot detection triggered")
            raise CookieExpiredError("403 Forbidden - cookies may be invalid")

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            logger.warning(f"Rate limited - retry after {retry_after}s")
            await self.rate_limiter.backoff(retry_after)
            raise RateLimitError(f"Rate limited, retry after {retry_after}s")

        response.raise_for_status()

        # Success - recover rate limiter
        await self.rate_limiter.recover()

        return response.json()

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
        Tries HTTP/2, falls back to HTTP/1.1.
        Auto-refreshes cookies on 403 errors.
        """

        async def attempt_search():
            """Single search attempt across HTTP versions"""
            for http_version in ["HTTP/2", "HTTP/1.1"]:
                try:
                    return await self._make_request(
                        origin, destination, date, passengers, search_type, http_version
                    )
                except (httpx.StreamError, httpx.HTTPStatusError) as e:
                    if http_version == "HTTP/2":
                        logger.warning(f"HTTP/2 failed, trying HTTP/1.1: {e}")
                        continue
                    raise

            return None

        async def on_retry_callback(attempt: int, error: Exception):
            """Handle retry attempts"""
            from .retry import classify_error
            from .models import ErrorType
            
            error_type = classify_error(error)

            if error_type == ErrorType.AUTH_FAILURE:
                logger.warning("Auth failure detected - refreshing cookies...")
                try:
                    await self.cookie_manager.get_cookies(
                        force_refresh=True, headless=True
                    )
                    logger.success("✓ Cookies refreshed")
                except Exception as e:
                    logger.error(f"Failed to refresh cookies: {e}")

            elif error_type == ErrorType.RATE_LIMIT:
                logger.warning("Rate limit detected - increasing backoff...")
                await self.rate_limiter.backoff(30)

        try:
            # Use circuit breaker for protection
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