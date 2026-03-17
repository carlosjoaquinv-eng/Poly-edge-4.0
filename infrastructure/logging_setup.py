"""
PolyEdge v4 — Logging Setup
============================
Configures rotating file + console logging.
Extracted from main_v4.py.
"""

import os
import logging
from logging.handlers import RotatingFileHandler


def setup_logging(config):
    """Configure logging with file rotation and console output."""
    os.makedirs(os.path.dirname(config.LOG_FILE) or "logs", exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # File handler (rotating, 10MB max, 5 backups)
    fh = RotatingFileHandler(config.LOG_FILE, maxBytes=10_000_000, backupCount=5)
    fh.setFormatter(fmt)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
    root.addHandler(fh)
    root.addHandler(ch)

    # Quiet noisy libraries
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
