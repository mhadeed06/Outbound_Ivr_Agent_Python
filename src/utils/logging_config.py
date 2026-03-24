import os
import logging
from logging.config import dictConfig

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

COLORS = {
    "DEBUG": "\033[37m",      # White
    "INFO": "\033[32m",       # Green
    "WARNING": "\033[33m",    # Yellow
    "ERROR": "\033[31m",      # Red
    "CRITICAL": "\033[41m",   # Red background
    "RESET": "\033[0m",       # Reset to default
}


class ColorFormatter(logging.Formatter):
    def format(self, record):
        log_color = COLORS.get(record.levelname, COLORS["RESET"])
        record.levelname = f"{log_color}{record.levelname}{COLORS['RESET']}"
        return super().format(record)


LOG_FORMAT = "[%(asctime)s - %(levelname)s - %(filename)s - %(funcName)s] %(message)s"

logging_config = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "colored": {
            "()": ColorFormatter,
            "format": LOG_FORMAT,
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "file": {
            "format": LOG_FORMAT,
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "default": {
            "level": "INFO",
            "formatter": "colored",
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["default"],
    },
}


def setup_logging():
    """Initialize the global logging configuration (console output).

    Call this once at application startup (in main.py).
    Per-session file handlers are added later via get_session_logger().
    """
    dictConfig(logging_config)


def get_session_logger(session_id: str) -> logging.Logger:
    """Return a logger dedicated to a specific WebSocket session.

    Creates (or reuses) a logger named ``session.<session_id>`` that
    writes to ``logs/<session_id>.log`` **and** to the console.

    The file handler uses the plain (non-colored) formatter so log files
    are human-readable without ANSI escape codes.
    """
    logger_name = f"session.{session_id}"
    logger = logging.getLogger(logger_name)

    # Avoid adding duplicate handlers on reconnection with the same session_id
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.propagate = True  # Also forward to root (console)

        # File handler — one log file per session
        log_path = os.path.join(LOGS_DIR, f"{session_id}.log")
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(file_handler)

    return logger


def cleanup_session_logger(session_id: str):
    """Close and remove file handlers for a disconnected session."""
    logger_name = f"session.{session_id}"
    logger = logging.getLogger(logger_name)
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
 