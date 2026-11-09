"""Small query helpers: one function per query, no ORM.

Timestamps go in as timezone-aware ``datetime`` values and are stored as fixed-width UTC
ISO-8601 text with milliseconds (``2026-11-09T10:00:00.000+00:00``), so sorting the text sorts
by time.
"""

import sqlite3
from datetime import UTC, datetime

INTERRUPTED_ERROR_TEXT = "interrupted: process stopped before the job finished"


def to_db_time(moment: datetime) -> str:
    """Format a timezone-aware ``moment`` as UTC ISO-8601 text; reject naive datetimes."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"timestamp must be timezone-aware, got naive {moment!r}")
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


# --- job_runs --------------------------------------------------------------------------


def start_job_run(conn: sqlite3.Connection, job: str, started_at: datetime) -> int:
    """Record that ``job`` started; return the new row's id. The row stays open until finished."""
    cursor = conn.execute(
        "INSERT INTO job_runs (job, started_at) VALUES (?, ?)", (job, to_db_time(started_at))
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def finish_job_run(
    conn: sqlite3.Connection,
    run_id: int,
    finished_at: datetime,
    *,
    ok: bool,
    items_processed: int | None = None,
    error_text: str | None = None,
) -> None:
    """Close the run ``run_id`` with its outcome; raise ``LookupError`` if there is no such run."""
    cursor = conn.execute(
        "UPDATE job_runs SET finished_at = ?, ok = ?, items_processed = ?, error_text = ? "
        "WHERE id = ?",
        (to_db_time(finished_at), int(ok), items_processed, error_text, run_id),
    )
    if cursor.rowcount != 1:
        raise LookupError(f"no job run with id {run_id}")


def close_interrupted_job_runs(conn: sqlite3.Connection, at: datetime) -> int:
    """Mark runs a previous process left open as failed (interrupted); return how many."""
    cursor = conn.execute(
        "UPDATE job_runs SET finished_at = ?, ok = 0, error_text = ? WHERE finished_at IS NULL",
        (to_db_time(at), INTERRUPTED_ERROR_TEXT),
    )
    return cursor.rowcount
