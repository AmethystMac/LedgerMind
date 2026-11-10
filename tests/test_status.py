"""The status report: mode, database, last heartbeat and last run per job, read-only."""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ledgermind import __version__
from ledgermind.config import AppConfig, Secrets, Settings
from ledgermind.db.migrate import LATEST_VERSION, open_database
from ledgermind.db.repo import JobRun, finish_job_run, start_job_run
from ledgermind.status import (
    STALE_HEARTBEAT_SECONDS,
    StatusError,
    StatusReport,
    format_age,
    format_status,
    read_status,
)

NOW = datetime(2026, 11, 9, 10, 5, 0, tzinfo=UTC)


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(
        settings=Settings(), secrets=Secrets(_env_file=None), config_path=Path("config.toml")
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "ledgermind.db"
    open_database(path).close()
    return path


def record(
    db_path: Path,
    job: str,
    started_at: datetime,
    *,
    ok: bool | None = True,
    items: int | None = 0,
    error: str | None = None,
) -> None:
    """Add one job run; ``ok=None`` leaves it running."""
    conn = open_database(db_path)
    try:
        run_id = start_job_run(conn, job, started_at)
        if ok is not None:
            finish_job_run(
                conn,
                run_id,
                started_at + timedelta(milliseconds=5),
                ok=ok,
                items_processed=items if ok else None,
                error_text=error,
            )
    finally:
        conn.close()


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- read_status -----------------------------------------------------------------------


def test_reads_mode_db_path_and_time(config: AppConfig, db_path: Path) -> None:
    report = read_status(config, db_path, NOW)

    assert report.mode == "paper"
    assert report.db_path == db_path
    assert report.now == NOW
    assert report.last_heartbeat is None
    assert report.job_runs == []


def test_reads_live_mode(db_path: Path) -> None:
    live = AppConfig(
        settings=Settings(mode="live"),
        secrets=Secrets(_env_file=None),
        config_path=Path("config.toml"),
    )

    assert read_status(live, db_path, NOW).mode == "live"


def test_reads_last_heartbeat_and_last_run_per_job(config: AppConfig, db_path: Path) -> None:
    record(db_path, "heartbeat", NOW - timedelta(seconds=120))
    record(db_path, "heartbeat", NOW - timedelta(seconds=60))
    record(db_path, "collector", NOW - timedelta(seconds=30), ok=False, error="boom")

    report = read_status(config, db_path, NOW)

    assert report.last_heartbeat is not None
    assert report.last_heartbeat.started_at == NOW - timedelta(seconds=60)
    assert [run.job for run in report.job_runs] == ["collector", "heartbeat"]
    assert report.job_runs[0].ok is False


def test_last_heartbeat_ignores_a_failed_one(config: AppConfig, db_path: Path) -> None:
    record(db_path, "heartbeat", NOW - timedelta(seconds=90))
    record(db_path, "heartbeat", NOW - timedelta(seconds=30), ok=False, error="boom")

    report = read_status(config, db_path, NOW)

    assert report.last_heartbeat is not None
    assert report.last_heartbeat.started_at == NOW - timedelta(seconds=90)


def test_missing_database_is_an_error_and_is_not_created(config: AppConfig, tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "ledgermind.db"

    with pytest.raises(StatusError, match="run"):
        read_status(config, db_path, NOW)

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_older_schema_is_an_error(config: AppConfig, db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION - 1}")
    conn.close()

    with pytest.raises(StatusError, match="version"):
        read_status(config, db_path, NOW)


def test_newer_schema_is_an_error(config: AppConfig, db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    conn.close()

    with pytest.raises(StatusError, match="version"):
        read_status(config, db_path, NOW)


def test_not_a_database_is_an_error(config: AppConfig, tmp_path: Path) -> None:
    db_path = tmp_path / "ledgermind.db"
    db_path.write_bytes(b"this is not sqlite" * 100)

    with pytest.raises(StatusError):
        read_status(config, db_path, NOW)


def test_does_not_change_the_database(config: AppConfig, db_path: Path) -> None:
    record(db_path, "heartbeat", NOW - timedelta(seconds=60))
    record(db_path, "collector", NOW - timedelta(seconds=10), ok=None)  # still running
    before = fingerprint(db_path)

    read_status(config, db_path, NOW)

    assert fingerprint(db_path) == before
    conn = sqlite3.connect(db_path)
    try:
        open_rows = conn.execute("SELECT count(*) FROM job_runs WHERE finished_at IS NULL")
        assert open_rows.fetchone()[0] == 1  # not closed as "interrupted"
    finally:
        conn.close()


def test_works_while_another_connection_holds_a_write_transaction(
    config: AppConfig, db_path: Path
) -> None:
    record(db_path, "heartbeat", NOW - timedelta(seconds=60))
    writer = open_database(db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        start_job_run(writer, "heartbeat", NOW)  # not committed yet

        report = read_status(config, db_path, NOW)

        assert report.last_heartbeat is not None
        assert report.last_heartbeat.started_at == NOW - timedelta(seconds=60)
        writer.execute("ROLLBACK")
    finally:
        writer.close()


# --- format_age ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 s ago"),
        (42, "42 s ago"),
        (59.9, "59 s ago"),
        (60, "1 min ago"),
        (5 * 60 + 30, "5 min ago"),
        (3 * 3600, "3 h ago"),
        (2 * 86400 + 5, "2 d ago"),
        (-5, "0 s ago"),  # a clock that jumped back is not "in the future"
    ],
)
def test_format_age(seconds: float, expected: str) -> None:
    assert format_age(timedelta(seconds=seconds)) == expected


# --- format_status ---------------------------------------------------------------------


def run(
    job: str,
    started_at: datetime,
    *,
    ok: bool | None = True,
    items: int | None = 0,
    error: str | None = None,
) -> JobRun:
    return JobRun(
        id=1,
        job=job,
        started_at=started_at,
        finished_at=None if ok is None else started_at,
        ok=ok,
        items_processed=items,
        error_text=error,
    )


def report(
    last_heartbeat: JobRun | None = None, job_runs: list[JobRun] | None = None
) -> StatusReport:
    return StatusReport(
        mode="paper",
        db_path=Path("data/ledgermind.db"),
        now=NOW,
        last_heartbeat=last_heartbeat,
        job_runs=job_runs or [],
    )


def test_format_shows_version_mode_and_database() -> None:
    text = format_status(report())

    assert f"LedgerMind {__version__}" in text
    assert "Mode:" in text
    assert "paper" in text
    assert str(Path("data/ledgermind.db")) in text


def test_format_without_any_runs() -> None:
    text = format_status(report())

    assert "Last heartbeat:  never" in text
    assert "no job runs yet" in text


def test_format_shows_heartbeat_in_utc_with_age() -> None:
    beat = run("heartbeat", NOW - timedelta(seconds=42))

    text = format_status(report(beat, [beat]))

    assert "Last heartbeat:  2026-11-09 10:04:18 UTC (42 s ago)" in text
    assert "stale" not in text


def test_format_flags_a_stale_heartbeat() -> None:
    beat = run("heartbeat", NOW - timedelta(seconds=STALE_HEARTBEAT_SECONDS + 1))

    text = format_status(report(beat, [beat]))

    assert "stale" in text
    assert "not running" in text


def test_heartbeat_just_inside_the_limit_is_not_stale() -> None:
    beat = run("heartbeat", NOW - timedelta(seconds=STALE_HEARTBEAT_SECONDS))

    assert "stale" not in format_status(report(beat, [beat]))


def test_stale_limit_allows_a_couple_of_missed_beats() -> None:
    assert STALE_HEARTBEAT_SECONDS >= 2 * 60


def test_format_lists_each_job_with_outcome() -> None:
    runs = [
        run("collector", NOW - timedelta(seconds=30), ok=False, items=None, error="Boom: x"),
        run("heartbeat", NOW - timedelta(seconds=42), ok=True, items=0),
        run("scorer", NOW - timedelta(seconds=5), ok=None, items=None),
        run("universe", NOW - timedelta(hours=3), ok=True, items=1),
        run("wallets", NOW - timedelta(hours=1), ok=True, items=12),
    ]

    lines = format_status(report(runs[1], runs)).splitlines()

    def line_for(job: str) -> str:
        (line,) = [line for line in lines if line.strip().startswith(job + " ")]
        return line

    assert "2026-11-09 10:04:30 UTC (30 s ago)" in line_for("collector")
    assert "FAILED" in line_for("collector")
    assert "Boom: x" in line_for("collector")
    assert "ok" in line_for("heartbeat")
    assert "0 items" in line_for("heartbeat")
    assert "running" in line_for("scorer")
    assert "1 item" in line_for("universe")
    assert "1 items" not in line_for("universe")
    assert "12 items" in line_for("wallets")
    order = [line.strip().split()[0] for line in lines if line.startswith("  ")]
    assert order == ["collector", "heartbeat", "scorer", "universe", "wallets"]


def test_format_shortens_long_errors_to_one_line() -> None:
    error = "ValueError: first line\nsecond line " + "x" * 500
    failed = run("collector", NOW, ok=False, items=None, error=error)

    text = format_status(report(None, [failed]))

    (line,) = [line for line in text.splitlines() if "collector" in line]
    assert "first line" in line
    assert "second line" not in text
    assert len(line) <= 160
