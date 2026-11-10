"""Small query helpers: one function per query, no ORM.

Timestamps go in as timezone-aware ``datetime`` values and are stored as fixed-width UTC
ISO-8601 text with milliseconds (``2026-11-09T10:00:00.000+00:00``), so sorting the text sorts
by time.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

INTERRUPTED_ERROR_TEXT = "interrupted: process stopped before the job finished"

_JOB_RUN_COLUMNS = "id, job, started_at, finished_at, ok, items_processed, error_text"


@dataclass(frozen=True)
class JobRun:
    """One row of ``job_runs``. ``finished_at`` and ``ok`` are ``None`` while it's running."""

    id: int
    job: str
    started_at: datetime
    finished_at: datetime | None
    ok: bool | None
    items_processed: int | None
    error_text: str | None


def to_db_time(moment: datetime) -> str:
    """Format a timezone-aware ``moment`` as UTC ISO-8601 text; reject naive datetimes."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"timestamp must be timezone-aware, got naive {moment!r}")
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


def from_db_time(text: str) -> datetime:
    """Read a timestamp written by ``to_db_time`` back as a timezone-aware UTC ``datetime``."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"stored timestamp has no timezone: {text!r}")
    return moment.astimezone(UTC)


def _optional_time(text: str | None) -> datetime | None:
    return None if text is None else from_db_time(text)


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


def latest_job_runs(conn: sqlite3.Connection) -> list[JobRun]:
    """The most recent run of each job (finished or not), sorted by job name."""
    rows = conn.execute(
        f"SELECT {_JOB_RUN_COLUMNS} FROM job_runs "
        "WHERE id IN (SELECT max(id) FROM job_runs GROUP BY job) ORDER BY job"
    ).fetchall()
    return [_job_run(row) for row in rows]


def last_successful_job_run(conn: sqlite3.Connection, job: str) -> JobRun | None:
    """The most recent run of ``job`` that finished with ``ok = 1``, or ``None``."""
    row = conn.execute(
        f"SELECT {_JOB_RUN_COLUMNS} FROM job_runs WHERE job = ? AND ok = 1 "
        "ORDER BY id DESC LIMIT 1",
        (job,),
    ).fetchone()
    return None if row is None else _job_run(row)


def _job_run(row: tuple[object, ...]) -> JobRun:
    run_id, job, started_at, finished_at, ok, items_processed, error_text = row
    assert isinstance(run_id, int)
    assert isinstance(job, str)
    assert isinstance(started_at, str)
    assert finished_at is None or isinstance(finished_at, str)
    assert ok is None or isinstance(ok, int)
    assert items_processed is None or isinstance(items_processed, int)
    assert error_text is None or isinstance(error_text, str)
    return JobRun(
        id=run_id,
        job=job,
        started_at=from_db_time(started_at),
        finished_at=_optional_time(finished_at),
        ok=None if ok is None else bool(ok),
        items_processed=items_processed,
        error_text=error_text,
    )
