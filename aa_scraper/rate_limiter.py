"""Adaptive rate limiter with exponential backoff"""

import asyncio

from loguru import logger


class AdaptiveRateLimiter:
    """
    Advanced rate limiter with exponential backoff and jitter.
    Adapts to rate limit errors automatically.
    """

    def __init__(self, rate: float = 1.0, burst: int = 3):
        """
        Initialize rate limiter.

        Args:
            rate: Base rate in requests per second
            burst: Maximum burst capacity (tokens)
        """
        self.base_rate = rate
        self.current_rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_update = asyncio.get_event_loop().time()
        self.lock = asyncio.Lock()
        self.backoff_until: Optional[float] = None

        logger.debug(f"Rate limiter initialized: {rate} req/s, burst={burst}")

    async def acquire(self) -> None:
        """Acquire a token with adaptive backoff"""
        async with self.lock:
            # Check if in backoff period
            if self.backoff_until:
                now = asyncio.get_event_loop().time()
                if now < self.backoff_until:
                    wait_time = self.backoff_until - now
                    logger.warning(f"Rate limiter in backoff: waiting {wait_time:.1f}s")
                    await asyncio.sleep(wait_time)
                self.backoff_until = None

            # Token bucket algorithm
            now = asyncio.get_event_loop().time()
            elapsed = now - self.last_update
            self.tokens = min(self.burst, self.tokens + elapsed * self.current_rate)
            self.last_update = now

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return

            wait_time = (1.0 - self.tokens) / self.current_rate
            await asyncio.sleep(wait_time)
            self.tokens = 0.0

    async def backoff(self, duration: float) -> None:
        """Enter backoff period (e.g., when rate limited)"""
        async with self.lock:
            self.backoff_until = asyncio.get_event_loop().time() + duration
            self.current_rate = max(0.1, self.current_rate * 0.5)  # Reduce rate
            logger.warning(
                f"Rate limited! Backing off for {duration:.1f}s, "
                f"new rate: {self.current_rate:.2f} req/s"
            )

    async def recover(self) -> None:
        """Recover rate limit after success"""
        async with self.lock:
            old_rate = self.current_rate
            self.current_rate = min(self.base_rate, self.current_rate * 1.2)
            if self.current_rate != old_rate:
                logger.info(
                    f"Rate limiter recovering: {old_rate:.2f} → {self.current_rate:.2f} req/s"
                )