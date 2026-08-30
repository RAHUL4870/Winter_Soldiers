"""Centralized logging configuration for NexaFreight Control Tower."""

from __future__ import annotations

import logging
import sys

from nexafreight.config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure application-wide structured logging.

    Sets up console logging with consistent format across all loggers,
    driven by the log level from settings.

    Args:
        settings: Application settings containing log_level configuration.
    """
    # Clear any existing handlers to avoid duplicates on reconfiguration
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Configure root logger
    root_logger.setLevel(settings.log_level)

    # Console handler with structured format
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(settings.log_level)

    # Format: timestamp | level | logger | message
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)

    # Suppress noisy third-party loggers
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("alembic").setLevel(logging.INFO)

    root_logger.info(
        f"Logging configured: level={settings.log_level}, environment={settings.environment}"
    )
