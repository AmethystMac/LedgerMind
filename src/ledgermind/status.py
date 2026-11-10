"""The ``status`` report: mode, database, last heartbeat and the last run of each job.

It only reads. The database is opened read-only (never created, migrated or tidied up), so
``status`` is safe to run while the bot is running in another terminal. Times are shown in UTC.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ledgermind import __version__
from ledgermind.app import HEARTBEAT_INTERVAL_SECONDS
from ledgermind.config import AppConfig, Mode
from ledgermind.db.migrate import BUSY_TIMEOUT_SECONDS, LATEST_VERSION
from ledgermind.db.repo import JobRun, last_successful_job_run, latest_job_runs

HEARTBEAT_JOB = "heartbeat"
# A heartbeat older than this means the bot has missed a few beats: it's probably not running.
STALE_HEARTBEAT_SECONDS = 3 * HEARTBEAT_INTERVAL_SECONDS
MAX_ERROR_CHARS = 80
TIME_FORMAT = "%Y-%m-%d %H:%M:%S UTC"


class StatusError(Exception):
    """The status can't be read. The message is meant for the user."""


@dataclass(frozen=True)
class StatusReport:
    """Everything ``status`` shows, read at ``now``."""

    mode: Mode
    db_path: Path
    now: datetime
    last_heartbeat: JobRun | None
    job_runs: list[JobRun]


def read_status(config: AppConfig, db_path: Path, now: datetime) -> StatusReport:
    """Read the report from ``db_path``; raise ``StatusError`` if the database can't be used."""
    if not db_path.is_file():
        raise StatusError(
            f"no database at {db_path}. Start the bot with `run` first (from the project folder)."
        )
    try:
        conn = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_SECONDS
        )
    except sqlite3.Error as exc:
        raise StatusError(f"{db_path}: cannot open database: {exc}") from None
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != LATEST_VERSION:
            raise StatusError(
                f"{db_path} is at schema version {version}, but this code expects version "
                f"{LATEST_VERSION}. Start the bot with `run` to update it, or update the code."
            )
        # One read transaction, so both queries see the same moment.
        conn.execute("BEGIN")
        last_heartbeat = last_successful_job_run(conn, HEARTBEAT_JOB)
        job_runs = latest_job_runs(conn)
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        raise StatusError(f"{db_path}: cannot read database: {exc}") from None
    finally:
        conn.close()
    return StatusReport(
        mode=config.settings.mode,
        db_path=db_path,
        now=now,
        last_heartbeat=last_heartbeat,
        job_runs=job_runs,
    )


def format_status(report: StatusReport) -> str:
    """Render ``report`` as plain text for the terminal."""
    lines = [
        f"LedgerMind {__version__}",
        f"Mode:            {report.mode}",
        f"Database:        {report.db_path}",
        f"Last heartbeat:  {_heartbeat_text(report)}",
        "",
        "Last run per job:",
    ]
    if not report.job_runs:
        lines.append("  (no job runs yet)")
    width = max((len(run.job) for run in report.job_runs), default=0)
    for run in report.job_runs:
        when = _when(run.started_at, report.now)
        lines.append(f"  {run.job:<{width}}  {when}  {_outcome(run)}".rstrip())
    return "\n".join(lines)


def format_age(age: timedelta) -> str:
    """A short, rounded-down age: ``42 s ago``, ``5 min ago``, ``3 h ago``, ``2 d ago``."""
    seconds = max(0, int(age.total_seconds()))
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _heartbeat_text(report: StatusReport) -> str:
    beat = report.last_heartbeat
    if beat is None:
        return "never"
    text = _when(beat.started_at, report.now)
    if report.now - beat.started_at > timedelta(seconds=STALE_HEARTBEAT_SECONDS):
        text += "  <- stale: the bot is probably not running"
    return text


def _when(moment: datetime, now: datetime) -> str:
    return f"{moment.astimezone(UTC).strftime(TIME_FORMAT)} ({format_age(now - moment)})"


def _outcome(run: JobRun) -> str:
    if run.ok is None:
        return "running"
    if not run.ok:
        return f"FAILED   {_short_error(run.error_text)}"
    items = run.items_processed
    if items is None:
        return "ok"
    return f"ok       {items} item{'' if items == 1 else 's'}"


def _short_error(error_text: str | None) -> str:
    if not error_text:
        return ""
    first_line = error_text.strip().splitlines()[0] if error_text.strip() else ""
    if len(first_line) > MAX_ERROR_CHARS:
        return first_line[: MAX_ERROR_CHARS - 3] + "..."
    return first_line
