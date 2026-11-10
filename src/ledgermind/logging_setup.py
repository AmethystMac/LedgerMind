"""Set up logging: console (stderr) plus a size-rotated file, level from config.

Every line starts with a UTC ISO-8601 timestamp, then the level, the logger name and the
message. Modules log through ``logging.getLogger(__name__)``; only the entrypoint calls
``setup_logging``. Calling it again replaces the handlers it added earlier, so lines are never
duplicated, and any other handlers on the root logger are left alone.

Short-lived commands such as ``status`` log to the console only (``log_dir=None``): they never
open the log file, so only the long-running bot ever rotates it.
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


def setup_logging(settings: LoggingSettings, log_dir: Path | None = DEFAULT_LOG_DIR) -> Path | None:
    """Send logs to stderr and to ``log_dir/ledgermind.log``; return the log file's path.

    With ``log_dir=None`` logs go to stderr only, no file is touched and ``None`` is returned.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / LOG_FILE_NAME
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=settings.max_file_bytes,
                backupCount=settings.backup_count,
                encoding="utf-8",
            )
        )

    formatter = UtcIsoFormatter(LOG_FORMAT)
    root = logging.getLogger()
    _remove_owned_handlers(root)
    for handler in handlers:
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
