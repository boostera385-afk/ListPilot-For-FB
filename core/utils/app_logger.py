import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path


_LOGGER_NAME = "kitkat.automation"
_CONFIGURE_LOCK = threading.Lock()


def _get_logger():
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger

    with _CONFIGURE_LOCK:
        if logger.handlers:
            return logger

        log_dir = Path(__file__).resolve().parents[2] / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "kitkat.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        return logger


def log_event(message, level=logging.INFO):
    """Persist one user-visible automation event without breaking the UI."""
    try:
        _get_logger().log(level, str(message).replace("\r", " "))
    except Exception:
        # Logging must never prevent an automation task from running.
        pass
