"""The job boundary: every run is recorded in job_runs, and a failing job never escapes."""

import asyncio
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ledgermind.db.migrate import open_database
from ledgermind.jobs import (
    CANCELLED_ERROR_TEXT,
    MAX_ERROR_TEXT_CHARS,
    heartbeat,
    run_recorded,
)

T0 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_database(tmp_path / "ledgermind.db")
    yield conn
    conn.close()


def runs(conn: sqlite3.Connection) -> list[dict[str, object]]:
    cursor = conn.execute("SELECT * FROM job_runs ORDER BY id")
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def job_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "ledgermind.jobs"]


class FakeClock:
    """Returns T0, T0 + 1 s, T0 + 2 s, ... on each call."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> datetime:
        value = T0 + timedelta(seconds=self.calls)
        self.calls += 1
        return value


# --- success ---------------------------------------------------------------------------


def test_success_is_recorded(db: sqlite3.Connection) -> None:
    async def job() -> int:
        return 5

    asyncio.run(run_recorded(db, "collector", job))

    [row] = runs(db)
    assert row["job"] == "collector"
    assert row["ok"] == 1
    assert row["items_processed"] == 5
    assert row["error_text"] is None
    assert row["finished_at"] is not None
    assert str(row["started_at"]) <= str(row["finished_at"])


def test_uses_the_given_clock(db: sqlite3.Connection) -> None:
    async def job() -> int:
        return 0

    asyncio.run(run_recorded(db, "heartbeat", job, now=FakeClock()))

    [row] = runs(db)
    assert row["started_at"] == "2026-11-09T10:00:00.000+00:00"
    assert row["finished_at"] == "2026-11-09T10:00:01.000+00:00"


def test_row_is_open_while_the_job_runs(db: sqlite3.Connection) -> None:
    seen: list[dict[str, object]] = []

    async def job() -> int:
        seen.extend(runs(db))
        return 0

    asyncio.run(run_recorded(db, "heartbeat", job))

    [during] = seen
    assert during["job"] == "heartbeat"
    assert during["finished_at"] is None
    assert during["ok"] is None


def test_success_logs_exactly_one_line(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ledgermind.jobs")

    async def job() -> int:
        return 3

    asyncio.run(run_recorded(db, "collector", job))

    [record] = job_records(caplog)
    assert record.levelno == logging.INFO
    message = record.getMessage()
    assert "collector" in message
    assert "ok" in message
    assert "3 item" in message


# --- failure ---------------------------------------------------------------------------


def test_exception_is_recorded_not_raised(db: sqlite3.Connection) -> None:
    async def job() -> int:
        raise ValueError("boom")

    asyncio.run(run_recorded(db, "collector", job))  # must not raise

    [row] = runs(db)
    assert row["ok"] == 0
    assert row["error_text"] == "ValueError: boom"
    assert row["items_processed"] is None
    assert row["finished_at"] is not None


def test_exception_is_logged_once_with_traceback(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ledgermind.jobs")

    async def job() -> int:
        raise ValueError("boom")

    asyncio.run(run_recorded(db, "collector", job))

    [record] = job_records(caplog)
    assert record.levelno == logging.ERROR
    assert "collector" in record.getMessage()
    assert "boom" in record.getMessage()
    assert record.exc_info is not None


def test_next_run_after_a_failure_still_works(db: sqlite3.Connection) -> None:
    calls = 0

    async def job() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first time fails")
        return 1

    asyncio.run(run_recorded(db, "collector", job))
    asyncio.run(run_recorded(db, "collector", job))

    assert [row["ok"] for row in runs(db)] == [0, 1]


def test_long_error_text_is_truncated(db: sqlite3.Connection) -> None:
    async def job() -> int:
        raise ValueError("x" * 10_000)

    asyncio.run(run_recorded(db, "collector", job))

    [row] = runs(db)
    error_text = str(row["error_text"])
    assert len(error_text) <= MAX_ERROR_TEXT_CHARS
    assert error_text.startswith("ValueError: xxx")


# --- cancellation (shutdown) -----------------------------------------------------------


def test_cancellation_is_recorded_and_reraised(db: sqlite3.Connection) -> None:
    started = asyncio.Event()

    async def job() -> int:
        started.set()
        await asyncio.sleep(60)
        return 0

    async def main() -> None:
        task = asyncio.create_task(run_recorded(db, "collector", job))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    [row] = runs(db)
    assert row["ok"] == 0
    assert row["error_text"] == CANCELLED_ERROR_TEXT
    assert row["finished_at"] is not None


# --- the job_runs write itself fails ---------------------------------------------------


def test_broken_database_does_not_raise_and_job_still_runs(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ledgermind.jobs")
    ran = False

    async def job() -> int:
        nonlocal ran
        ran = True
        return 0

    db.close()

    asyncio.run(run_recorded(db, "heartbeat", job))  # must not raise

    assert ran
    assert any(
        r.levelno >= logging.ERROR and "job_runs" in r.getMessage() for r in job_records(caplog)
    )


# --- heartbeat -------------------------------------------------------------------------


def test_heartbeat_processes_nothing() -> None:
    assert asyncio.run(heartbeat()) == 0


def test_heartbeat_run_is_recorded_ok(db: sqlite3.Connection) -> None:
    asyncio.run(run_recorded(db, "heartbeat", heartbeat))

    [row] = runs(db)
    assert (row["job"], row["ok"], row["items_processed"]) == ("heartbeat", 1, 0)
