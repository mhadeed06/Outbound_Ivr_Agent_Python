import os
import contextvars
import logging
from logging.config import dictConfig

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

COLORS = {
    "DEBUG": "\033[37m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[41m",
    "RESET": "\033[0m",
}


# ── Per-call context tag ────────────────────────────────────────────────────
# Set once at each call entry point (orchestrate, webhooks, stream, shutdown).
# asyncio.create_task inherits the current context, so background tasks —
# including the post-call upload — automatically carry the tag without
# any extra wiring.
#
# To filter logs for one call:   grep "DthoBVG0" app.log
# The short tag is the first 8 characters of the Telnyx call_control_id
# AFTER the "v3:" prefix, which is stable and unique per call.
# ────────────────────────────────────────────────────────────────────────────
_call_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("call_id", default="-")

SHORT_TAG_WIDTH = 8


def _shorten(call_control_id: str) -> str:
    """Turn a long call_control_id (e.g. 'v3:DthoBVG07lIX...') into a short
    tag for log lines (e.g. 'DthoBVG0'). The mapping is deterministic so
    you can always grep by the same short tag."""
    if not call_control_id:
        return "-"
    trimmed = call_control_id.split(":", 1)[-1]
    return trimmed[:SHORT_TAG_WIDTH] or "-"


def set_call_id(call_control_id: str) -> None:
    """Set the current call tag. Every subsequent log line on this task
    (and any asyncio task it spawns) will include this tag."""
    _call_id_var.set(_shorten(call_control_id))


def get_call_id() -> str:
    return _call_id_var.get()


def shorten_call_id(call_control_id: str) -> str:
    """Public: turn a full call_control_id into the same short tag used in
    log lines. Use this when you have the id but not the ContextVar (e.g. in
    a detached background task) and need a value that matches the logs."""
    return _shorten(call_control_id)


class CallIdFilter(logging.Filter):
    """Injects the current call's short tag onto every log record."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.call_id = _call_id_var.get()
        return True


class ColorFormatter(logging.Formatter):
    def format(self, record):
        log_color = COLORS.get(record.levelname, COLORS["RESET"])
        # Colour only the levelname field — leave call_id plain so it's grep-friendly.
        record.levelname = f"{log_color}{record.levelname}{COLORS['RESET']}"
        return super().format(record)


LOG_FORMAT = "[%(asctime)s - %(levelname)s - %(call_id)-8s - %(filename)s - %(funcName)s] %(message)s"

logging_config = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "call_id": {
            "()": CallIdFilter,
        },
    },
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
            "filters": ["call_id"],
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

    # Quiet noisy third-party loggers that print full URLs of every HTTP call
    # (would leak endpoint URLs — and sometimes path-embedded IDs — to logs).
    for noisy in ("httpx", "httpcore", "urllib3", "azure"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_session_logger(session_id: str) -> logging.Logger:
    """Return a logger dedicated to a specific WebSocket session."""
    logger_name = f"session.{session_id}"
    logger = logging.getLogger(logger_name)

    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.propagate = True

        log_path = os.path.join(LOGS_DIR, f"{session_id}.log")
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        file_handler.addFilter(CallIdFilter())
        logger.addHandler(file_handler)

    return logger


def cleanup_session_logger(session_id: str):
    """Close and remove file handlers for a disconnected session."""
    logger_name = f"session.{session_id}"
    logger = logging.getLogger(logger_name)
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
