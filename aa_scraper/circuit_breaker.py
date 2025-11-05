"""Circuit breaker pattern implementation"""

import asyncio
from datetime import datetime

from loguru import logger

from .exceptions import CircuitOpenError
from .models import CircuitState


class CircuitBreaker:
    """
    Circuit breaker pattern to prevent cascading failures.
    Opens after threshold failures, closes after timeout.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        timeout: float = 300.0,
        name: str = "default",
    ):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            timeout: Seconds before attempting to close circuit
            name: Name for logging purposes
        """
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.name = name
        self.failures = 0
        self.state = CircuitState.CLOSED
        self.last_failure_time: Optional[float] = None
        self.lock = asyncio.Lock()

        logger.debug(
            f"Circuit breaker '{name}' initialized: "
            f"threshold={failure_threshold}, timeout={timeout}s"
        )

    async def call(self, func, *args, **kwargs):
        """Execute function with circuit breaker protection"""
        async with self.lock:
            # Check if circuit should transition from OPEN to HALF_OPEN
            if self.state == CircuitState.OPEN:
                if self.last_failure_time:
                    elapsed = datetime.now().timestamp() - self.last_failure_time
                    if elapsed >= self.timeout:
                        logger.info(
                            f"Circuit '{self.name}' transitioning to HALF_OPEN "
                            f"(timeout expired)"
                        )
                        self.state = CircuitState.HALF_OPEN
                        self.failures = 0
                    else:
                        remaining = self.timeout - elapsed
                        raise CircuitOpenError(
                            f"Circuit '{self.name}' is OPEN, retry in {remaining:.0f}s"
                        )

        try:
            result = await func(*args, **kwargs)

            # Success - close circuit if it was half-open
            async with self.lock:
                if self.state == CircuitState.HALF_OPEN:
                    logger.success(f"Circuit '{self.name}' recovered, closing")
                    self.state = CircuitState.CLOSED
                    self.failures = 0

            return result

        except Exception as e:
            async with self.lock:
                self.failures += 1
                self.last_failure_time = datetime.now().timestamp()

                if self.failures >= self.failure_threshold:
                    logger.error(
                        f"Circuit '{self.name}' OPENING after {self.failures} failures"
                    )
                    self.state = CircuitState.OPEN
                else:
                    logger.warning(
                        f"Circuit '{self.name}' failure "
                        f"{self.failures}/{self.failure_threshold}"
                    )

            raise