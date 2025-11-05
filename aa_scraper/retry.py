"""Retry logic with exponential backoff"""

import asyncio
import random
from typing import Callable, Optional

import httpx
from loguru import logger

from .config import (
    BACKOFF_MULTIPLIER,
    INITIAL_BACKOFF,
    JITTER_RANGE,
    MAX_BACKOFF,
    MAX_RETRIES,
)
from .exceptions import CookieExpiredError, RateLimitError
from .models import ErrorType


async def retry_with_backoff(
    func: Callable,
    *args,
    max_retries: int = MAX_RETRIES,
    initial_backoff: float = INITIAL_BACKOFF,
    max_backoff: float = MAX_BACKOFF,
    backoff_multiplier: float = BACKOFF_MULTIPLIER,
    on_retry: Optional[Callable] = None,
    **kwargs,
):
    """
    Execute function with exponential backoff retry logic.

    Args:
        func: Async function to execute
        max_retries: Maximum number of retry attempts
        initial_backoff: Initial backoff duration in seconds
        max_backoff: Maximum backoff duration
        backoff_multiplier: Multiplier for exponential backoff
        on_retry: Optional callback called on each retry: on_retry(attempt, error)
    """
    last_exception = None
    backoff = initial_backoff

    for attempt in range(max_retries + 1):
        try:
            result = await func(*args, **kwargs)

            # Success - log recovery if this was a retry
            if attempt > 0:
                logger.success(f"✓ Recovered after {attempt} retries")

            return result

        except Exception as e:
            last_exception = e

            # Check if we should retry
            if attempt >= max_retries:
                logger.error(f"❌ Failed after {max_retries} retries: {e}")
                break

            # Calculate backoff with jitter
            jitter = random.uniform(*JITTER_RANGE)
            sleep_time = min(backoff * jitter, max_backoff)

            error_type = classify_error(e)
            logger.warning(
                f"⚠️ Attempt {attempt + 1}/{max_retries + 1} failed "
                f"({error_type.value}): {e}"
            )
            logger.info(f"   Retrying in {sleep_time:.1f}s...")

            # Call retry callback if provided
            if on_retry:
                await on_retry(attempt, e)

            await asyncio.sleep(sleep_time)
            backoff *= backoff_multiplier

    raise last_exception


def classify_error(error: Exception) -> ErrorType:
    """Classify error for appropriate handling"""
    if isinstance(error, CookieExpiredError):
        return ErrorType.AUTH_FAILURE
    elif isinstance(error, RateLimitError):
        return ErrorType.RATE_LIMIT
    elif isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code == 403:
            return ErrorType.AUTH_FAILURE
        elif error.response.status_code == 429:
            return ErrorType.RATE_LIMIT
        elif error.response.status_code >= 500:
            return ErrorType.TRANSIENT
        else:
            return ErrorType.PERMANENT
    elif isinstance(error, (httpx.ConnectError, httpx.TimeoutException)):
        return ErrorType.TRANSIENT
    else:
        return ErrorType.PERMANENT