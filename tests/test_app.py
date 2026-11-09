"""The app runner: scheduler + heartbeat, jobs recorded, clean shutdown on Ctrl+C / SIGTERM."""

import asyncio
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from apscheduler.triggers.interval import IntervalTrigger

from ledgermind.app import (
    HEARTBEAT_INTERVAL_SECONDS,
    ScheduledJob,
    default_jobs,
    main,
    run_app,
)
from ledgermind.config import AppConfig, Secrets, Settings
from ledgermind.db.migrate import LATEST_VERSION, open_database
from ledgermind.db.repo import INTERRUPTED_ERROR_TEXT, start_job_run
from ledgermind.jobs import CANCELLED_ERROR_TEXT, heartbeat

FAST = 0.05  # seconds between runs in the scheduler tests
TIMEOUT = 10.0
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """main() sets up logging; undo it and close its files after each test."""
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers_before:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level_before)


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(
        settings=Settings(), secrets=Secrets(_env_file=None), config_path=Path("config.toml")
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "ledgermind.db"


def every(seconds: float) -> IntervalTrigger:
    return IntervalTrigger(seconds=seconds, timezone=UTC)


def runs(db_path: Path, job: str | None = None) -> list[dict[str, object]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        sql = "SELECT * FROM job_runs"
        params: tuple[str, ...] = ()
        if job is not None:
            sql += " WHERE job = ?"
            params = (job,)
        try:
            cursor = conn.execute(sql + " ORDER BY id", params)
        except sqlite3.OperationalError:  # table not created yet
            return []
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    finally:
        conn.close()


def finished_runs(db_path: Path, job: str) -> list[dict[str, object]]:
    return [row for row in runs(db_path, job) if row["finished_at"] is not None]


def run_until(
    config: AppConfig,
    db_path: Path,
    jobs: Sequence[ScheduledJob],
    condition: Callable[[], bool],
    **kwargs: float,
) -> None:
    """Run the app until ``condition()`` is true, then ask it to stop and wait for it."""

    async def scenario() -> None:
        stop = asyncio.Event()

        async def watch() -> None:
            while not condition():
                await asyncio.sleep(0.02)
            stop.set()

        watcher = asyncio.create_task(watch())
        try:
            await asyncio.wait_for(
                run_app(config, db_path, jobs=jobs, stop_event=stop, **kwargs), TIMEOUT
            )
        finally:
            watcher.cancel()

    asyncio.run(scenario())


# --- default wiring --------------------------------------------------------------------


def test_heartbeat_runs_every_60_seconds_starting_now() -> None:
    [job] = default_jobs()

    assert job.name == "heartbeat"
    assert job.func is heartbeat
    assert HEARTBEAT_INTERVAL_SECONDS == 60
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(seconds=60)
    assert job.run_at_start


# --- scheduler -------------------------------------------------------------------------


def test_heartbeats_are_recorded_until_stopped(config: AppConfig, db_path: Path) -> None:
    jobs = [ScheduledJob("heartbeat", heartbeat, every(FAST))]

    run_until(config, db_path, jobs, lambda: len(finished_runs(db_path, "heartbeat")) >= 3)

    rows = runs(db_path, "heartbeat")
    assert len(rows) >= 3
    assert all(row["ok"] == 1 and row["items_processed"] == 0 for row in rows)


def test_job_runs_once_at_start_when_asked(config: AppConfig, db_path: Path) -> None:
    # A one-hour interval: the only run we can see is the one at start.
    jobs = [ScheduledJob("heartbeat", heartbeat, every(3600))]

    run_until(config, db_path, jobs, lambda: len(finished_runs(db_path, "heartbeat")) >= 1)

    assert len(runs(db_path, "heartbeat")) == 1


def test_job_waits_for_its_trigger_when_not_run_at_start(config: AppConfig, db_path: Path) -> None:
    ran_at: list[float] = []
    start = time.monotonic()

    async def job() -> int:
        ran_at.append(time.monotonic() - start)
        return 0

    jobs = [ScheduledJob("later", job, every(0.3), run_at_start=False)]

    run_until(config, db_path, jobs, lambda: bool(ran_at))

    assert ran_at[0] >= 0.25


def test_failing_job_does_not_stop_the_others(config: AppConfig, db_path: Path) -> None:
    async def broken() -> int:
        raise RuntimeError("always fails")

    jobs = [
        ScheduledJob("broken", broken, every(FAST)),
        ScheduledJob("heartbeat", heartbeat, every(FAST)),
    ]

    run_until(
        config,
        db_path,
        jobs,
        lambda: (
            len(finished_runs(db_path, "broken")) >= 3
            and len(finished_runs(db_path, "heartbeat")) >= 3
        ),
    )

    assert all(row["ok"] == 0 for row in runs(db_path, "broken"))
    assert all(row["error_text"] == "RuntimeError: always fails" for row in runs(db_path, "broken"))
    assert all(row["ok"] == 1 for row in runs(db_path, "heartbeat"))


def test_start_and_stop_are_logged(
    config: AppConfig, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    jobs = [ScheduledJob("heartbeat", heartbeat, every(FAST))]

    run_until(config, db_path, jobs, lambda: bool(finished_runs(db_path, "heartbeat")))

    messages = [r.getMessage() for r in caplog.records if r.name == "ledgermind.app"]
    assert any("starting" in m and "paper" in m for m in messages)
    assert "stopped cleanly" in messages[-1]


# --- startup: runs left open by a crash ------------------------------------------------


def test_runs_left_open_by_a_crash_are_closed_at_start(config: AppConfig, db_path: Path) -> None:
    conn = open_database(db_path)
    start_job_run(conn, "collector", datetime(2026, 11, 8, 9, 0, tzinfo=UTC))
    conn.close()
    jobs = [ScheduledJob("heartbeat", heartbeat, every(FAST))]

    run_until(config, db_path, jobs, lambda: bool(finished_runs(db_path, "heartbeat")))

    [old] = runs(db_path, "collector")
    assert old["ok"] == 0
    assert old["error_text"] == INTERRUPTED_ERROR_TEXT
    assert old["finished_at"] is not None


# --- shutdown --------------------------------------------------------------------------


def test_shutdown_waits_for_a_running_job(config: AppConfig, db_path: Path) -> None:
    started = False

    async def slow() -> int:
        nonlocal started
        started = True
        await asyncio.sleep(0.3)
        return 7

    jobs = [ScheduledJob("slow", slow, every(3600))]

    run_until(config, db_path, jobs, lambda: started, shutdown_grace_seconds=5)

    [row] = runs(db_path, "slow")
    assert row["ok"] == 1
    assert row["items_processed"] == 7


def test_job_still_running_after_the_grace_period_is_cancelled(
    config: AppConfig, db_path: Path
) -> None:
    started = False

    async def stuck() -> int:
        nonlocal started
        started = True
        await asyncio.sleep(60)
        return 0

    jobs = [ScheduledJob("stuck", stuck, every(3600))]
    began = time.monotonic()

    run_until(config, db_path, jobs, lambda: started, shutdown_grace_seconds=0.2)

    assert time.monotonic() - began < 5
    [row] = runs(db_path, "stuck")
    assert row["ok"] == 0
    assert row["error_text"] == CANCELLED_ERROR_TEXT


@POSIX_ONLY
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_signal_stops_the_app_and_restores_handlers(
    config: AppConfig, db_path: Path, sig: signal.Signals
) -> None:
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    jobs = [ScheduledJob("heartbeat", heartbeat, every(FAST))]

    async def scenario() -> None:
        async def send_after_first_heartbeat() -> None:
            while not finished_runs(db_path, "heartbeat"):
                await asyncio.sleep(0.02)
            os.kill(os.getpid(), sig)

        sender = asyncio.create_task(send_after_first_heartbeat())
        await asyncio.wait_for(run_app(config, db_path, jobs=jobs), TIMEOUT)
        await sender

    asyncio.run(scenario())

    assert all(row["finished_at"] is not None for row in runs(db_path))
    assert {s: signal.getsignal(s) for s in before} == before


# --- main(): errors exit with a message, not a traceback -------------------------------


def test_newer_database_exits_with_message(
    tmp_path: Path, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_path.parent.mkdir(parents=True)
    plain = sqlite3.connect(db_path)
    plain.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    plain.close()
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main(config_path, tmp_path / ".env", db_path, tmp_path / "logs")

    assert excinfo.value.code == 1
    assert any(r.levelno == logging.CRITICAL and "newer" in r.getMessage() for r in caplog.records)


def test_missing_config_exits_with_message(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(tmp_path / "config.toml", tmp_path / ".env", tmp_path / "db", tmp_path / "logs")

    assert isinstance(excinfo.value.code, str)
    assert "config.toml" in excinfo.value.code


# --- the real process, end to end ------------------------------------------------------


@POSIX_ONLY
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_real_process_stops_cleanly_on_signal(tmp_path: Path, sig: signal.Signals) -> None:
    (tmp_path / "config.toml").write_text('mode = "paper"\n', encoding="utf-8")
    db_path = tmp_path / "data" / "ledgermind.db"
    process = subprocess.Popen(
        [sys.executable, "-c", "from ledgermind.app import main; main()"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + TIMEOUT
        while not finished_runs(db_path, "heartbeat"):
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "no heartbeat recorded in time"
            time.sleep(0.05)

        process.send_signal(sig)
        _, stderr = process.communicate(timeout=TIMEOUT)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0, stderr
    assert "Traceback" not in stderr
    assert "heartbeat: ok" in stderr
    assert "stopped cleanly" in stderr
    assert "Running job" not in stderr  # APScheduler's own per-run lines are quieted
    log_text = (tmp_path / "logs" / "ledgermind.log").read_text(encoding="utf-8")
    assert "heartbeat: ok" in log_text
    assert "stopped cleanly" in log_text
    assert all(row["finished_at"] is not None for row in runs(db_path))
