"""Logging configuration using loguru"""

import sys
from pathlib import Path
from typing import Optional

from loguru import logger


def setup_logging(verbose: bool = False, log_file: Optional[Path] = None) -> None:
    """
    Configure loguru for production logging.

    Args:
        verbose: Enable debug-level logging
        log_file: Optional file path for log output
    """
    # Remove default handler
    logger.remove()

    # Console handler with colors
    log_level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level=log_level,
        colorize=True,
    )

    # File handler with rotation
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_file,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
            level="DEBUG",
            rotation="100 MB",
            retention="30 days",
            compression="zip",
            enqueue=True,  # Thread-safe
        )
        logger.info(f"Logging to file: {log_file}")