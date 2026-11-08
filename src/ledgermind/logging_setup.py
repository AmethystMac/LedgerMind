"""Set up logging: console (stderr) plus a size-rotated file, level from config.

Every line starts with a UTC ISO-8601 timestamp, then the level, the logger name and the
message. Modules log through ``logging.getLogger(__name__)``; only the entrypoint calls
``setup_logging``. Calling it again replaces the handlers it added earlier, so lines are never
duplicated, and any other handlers on the root logger are left alone.
"""

import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ledgermind.config import LoggingSettings

DEFAULT_LOG_DIR = Path("logs")
LOG_FILE_NAME = "ledgermind.log"
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Marks the handlers this module installed, so a repeat call removes only those.
_OWNED_ATTR = "_ledgermind_owned"


class UtcIsoFormatter(logging.Formatter):
    """Formats ``asctime`` as UTC ISO-8601 with milliseconds, e.g. ``...T12:00:00.123+00:00``."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")


def setup_logging(settings: LoggingSettings, log_dir: Path = DEFAULT_LOG_DIR) -> Path:
    """Send logs to stderr and to ``log_dir/ledgermind.log``; return the log file's path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_FILE_NAME

    formatter = UtcIsoFormatter(LOG_FORMAT)
    console = logging.StreamHandler(sys.stderr)
    file = RotatingFileHandler(
        log_file,
        maxBytes=settings.max_file_bytes,
        backupCount=settings.backup_count,
        encoding="utf-8",
    )

    root = logging.getLogger()
    _remove_owned_handlers(root)
    for handler in (console, file):
        handler.setFormatter(formatter)
        setattr(handler, _OWNED_ATTR, True)
        root.addHandler(handler)
    root.setLevel(settings.level)
    return log_file


def _remove_owned_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, _OWNED_ATTR, False):
            logger.removeHandler(handler)
            handler.close()
