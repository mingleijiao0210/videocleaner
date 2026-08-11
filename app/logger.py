"""本地滚动日志。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .settings import writable_root


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("VideoCleaner")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    log_dir = writable_root() / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "videocleaner.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    except OSError:
        # Importing the application must remain possible in read-only sandboxes
        # and CI. File logging is best-effort; stderr is safe for diagnostics.
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )
    logger.addHandler(handler)
    return logger


LOGGER = setup_logger()
