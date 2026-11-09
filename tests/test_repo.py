"""Query helpers for job_runs: open a run, finish it, and close runs a crash left open."""

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ledgermind.db.migrate import open_database
from ledgermind.db.repo import (
    INTERRUPTED_ERROR_TEXT,
    close_interrupted_job_runs,
    finish_job_run,
    start_job_run,
)

T0 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(seconds=2, milliseconds=345)
UTC_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00$")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_database(tmp_path / "ledgermind.db")
    yield conn
    conn.close()


def job_run(conn: sqlite3.Connection, run_id: int) -> dict[str, object]:
    cursor = conn.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,))
    names = [column[0] for column in cursor.description]
    row = cursor.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


# --- start_job_run ---------------------------------------------------------------------


def test_start_adds_an_open_row(db: sqlite3.Connection) -> None:
    run_id = start_job_run(db, "heartbeat", T0)

    assert job_run(db, run_id) == {
        "id": run_id,
        "job": "heartbeat",
        "started_at": "2026-11-09T10:00:00.000+00:00",
        "finished_at": None,
        "ok": None,
        "error_text": None,
        "items_processed": None,
    }


def test_each_start_gets_its_own_row(db: sqlite3.Connection) -> None:
    first = start_job_run(db, "heartbeat", T0)
    second = start_job_run(db, "heartbeat", T1)

    assert first != second
    assert db.execute("SELECT count(*) FROM job_runs").fetchone()[0] == 2


# --- finish_job_run --------------------------------------------------------------------


def test_finish_records_success(db: sqlite3.Connection) -> None:
    run_id = start_job_run(db, "collector", T0)

    finish_job_run(db, run_id, T1, ok=True, items_processed=42)

    row = job_run(db, run_id)
    assert row["finished_at"] == "2026-11-09T10:00:02.345+00:00"
    assert row["ok"] == 1
    assert row["items_processed"] == 42
    assert row["error_text"] is None


def test_finish_records_failure(db: sqlite3.Connection) -> None:
    run_id = start_job_run(db, "collector", T0)

    finish_job_run(db, run_id, T1, ok=False, error_text="TimeoutError: no reply")

    row = job_run(db, run_id)
    assert row["ok"] == 0
    assert row["error_text"] == "TimeoutError: no reply"
    assert row["items_processed"] is None


def test_finish_unknown_run_raises(db: sqlite3.Connection) -> None:
    with pytest.raises(LookupError):
        finish_job_run(db, 999, T1, ok=True, items_processed=0)


# --- timestamps ------------------------------------------------------------------------


def test_timestamps_are_fixed_width_utc_with_milliseconds(db: sqlite3.Connection) -> None:
    # Fixed width means sorting the text sorts by time.
    run_id = start_job_run(db, "heartbeat", T0.replace(microsecond=123_456))
    finish_job_run(db, run_id, T1, ok=True, items_processed=0)

    row = job_run(db, run_id)
    assert row["started_at"] == "2026-11-09T10:00:00.123+00:00"
    assert UTC_MS.match(str(row["finished_at"]))


def test_other_timezones_are_stored_as_utc(db: sqlite3.Connection) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))

    run_id = start_job_run(db, "heartbeat", datetime(2026, 11, 9, 15, 30, tzinfo=ist))

    assert job_run(db, run_id)["started_at"] == "2026-11-09T10:00:00.000+00:00"


def test_naive_datetime_is_rejected(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="timezone"):
        start_job_run(db, "heartbeat", datetime(2026, 11, 9, 10, 0))  # noqa: DTZ001


# --- close_interrupted_job_runs --------------------------------------------------------


def test_close_interrupted_closes_only_open_rows(db: sqlite3.Connection) -> None:
    open_one = start_job_run(db, "heartbeat", T0)
    open_two = start_job_run(db, "collector", T0)
    finished = start_job_run(db, "heartbeat", T0)
    finish_job_run(db, finished, T0, ok=True, items_processed=0)
    finished_before = job_run(db, finished)

    closed = close_interrupted_job_runs(db, T1)

    assert closed == 2
    for run_id in (open_one, open_two):
        row = job_run(db, run_id)
        assert row["ok"] == 0
        assert row["finished_at"] == "2026-11-09T10:00:02.345+00:00"
        assert row["error_text"] == INTERRUPTED_ERROR_TEXT
        assert row["items_processed"] is None
    assert job_run(db, finished) == finished_before


def test_interrupted_text_says_what_happened() -> None:
    assert "interrupted" in INTERRUPTED_ERROR_TEXT.lower()


def test_close_interrupted_with_nothing_open_returns_zero(db: sqlite3.Connection) -> None:
    assert close_interrupted_job_runs(db, T1) == 0
