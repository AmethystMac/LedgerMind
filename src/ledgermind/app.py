"""Wire the app together: database, scheduler, jobs, and a clean shutdown.

``main()`` is the entrypoint: it loads config, sets up logging and runs ``run_app`` until
Ctrl+C (SIGINT) or SIGTERM. ``run_app`` opens and migrates the database, closes any job runs a
previous process left open, starts the scheduler, and on a stop request lets running jobs
finish (up to a grace period) before closing everything.

Jobs are ``async`` functions and run on the event loop, the same thread that owns the database
connection, so they can share it.
"""

import asyncio
import logging
import signal
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ledgermind import __version__
from ledgermind.config import DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH, AppConfig, load_config_or_exit
from ledgermind.db.migrate import DEFAULT_DB_PATH, MigrationError, open_database
from ledgermind.db.repo import close_interrupted_job_runs
from ledgermind.jobs import Job, heartbeat, run_recorded, utc_now
from ledgermind.logging_setup import DEFAULT_LOG_DIR, setup_logging

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 60
# A run that starts this late (e.g. after the machine slept) still runs; later than that it's
# skipped and the next one is waited for. Missed runs of the same job are merged into one.
MISFIRE_GRACE_SECONDS = 30
# On stop, how long running jobs get to finish before they're cancelled.
SHUTDOWN_GRACE_SECONDS = 10.0
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


@dataclass(frozen=True)
class ScheduledJob:
    """A job, when it runs, and whether it also runs once right at start-up."""

    name: str
    func: Job
    trigger: BaseTrigger
    run_at_start: bool = True


def default_jobs() -> list[ScheduledJob]:
    """The jobs the app runs. Later phases add theirs here."""
    return [
        ScheduledJob(
            "heartbeat",
            heartbeat,
            IntervalTrigger(seconds=HEARTBEAT_INTERVAL_SECONDS, timezone=UTC),
        ),
    ]


def main(
    config_path: Path = DEFAULT_CONFIG_PATH,
    env_path: Path = DEFAULT_ENV_PATH,
    db_path: Path = DEFAULT_DB_PATH,
    log_dir: Path = DEFAULT_LOG_DIR,
) -> None:
    """Run the bot until Ctrl+C or SIGTERM. Exits with a message, not a traceback, on errors."""
    config = load_config_or_exit(config_path, env_path)
    setup_logging(config.settings.logging, log_dir)
    # APScheduler logs two INFO lines per job run; our own one line per run is enough.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    try:
        asyncio.run(run_app(config, db_path))
    except MigrationError as exc:
        logger.critical("Cannot start: %s", exc)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        # Ctrl+C before the signal handlers were installed (i.e. during start-up).
        logger.info("Interrupted during start-up")
        raise SystemExit(130) from None
    except Exception:
        logger.critical("LedgerMind crashed", exc_info=True)
        raise SystemExit(1) from None


async def run_app(
    config: AppConfig,
    db_path: Path = DEFAULT_DB_PATH,
    *,
    jobs: Sequence[ScheduledJob] | None = None,
    stop_event: asyncio.Event | None = None,
    shutdown_grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
) -> None:
    """Run the scheduler until ``stop_event`` is set or a stop signal arrives."""
    stop = stop_event if stop_event is not None else asyncio.Event()
    with _stop_on_signals(stop):
        conn = open_database(db_path)
        try:
            interrupted = close_interrupted_job_runs(conn, utc_now())
            if interrupted:
                logger.warning(
                    "Marked %d job run(s) left open by the last process as interrupted",
                    interrupted,
                )
            logger.info(
                "LedgerMind %s starting in %s mode, database %s",
                __version__,
                config.settings.mode,
                db_path,
            )
            running: set[asyncio.Task[None]] = set()
            scheduler = _build_scheduler()
            for job in default_jobs() if jobs is None else jobs:
                _add_job(scheduler, conn, job, running)
            scheduler.start()
            try:
                await stop.wait()
            finally:
                await _shutdown(scheduler, running, shutdown_grace_seconds)
        finally:
            conn.close()
    logger.info("LedgerMind stopped cleanly")


def _build_scheduler() -> AsyncIOScheduler:
    return AsyncIOScheduler(
        timezone=UTC,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": MISFIRE_GRACE_SECONDS,
        },
    )


def _add_job(
    scheduler: AsyncIOScheduler,
    conn: sqlite3.Connection,
    job: ScheduledJob,
    running: set[asyncio.Task[None]],
) -> None:
    async def scheduled() -> None:
        task = asyncio.current_task()
        assert task is not None
        running.add(task)
        try:
            await run_recorded(conn, job.name, job.func)
        finally:
            running.discard(task)

    options: dict[str, Any] = {}
    if job.run_at_start:
        options["next_run_time"] = datetime.now(UTC)
    scheduler.add_job(scheduled, job.trigger, id=job.name, name=job.name, **options)


async def _shutdown(
    scheduler: AsyncIOScheduler, running: set[asyncio.Task[None]], grace_seconds: float
) -> None:
    """Start no new runs, give running ones ``grace_seconds`` to finish, then cancel the rest."""
    scheduler.pause()
    await asyncio.sleep(0)  # let runs that were just handed to the loop actually start
    if running:
        logger.info(
            "Stopping: waiting up to %g s for %d running job(s)", grace_seconds, len(running)
        )
        await asyncio.wait(list(running), timeout=grace_seconds)
    scheduler.shutdown(wait=False)  # cancels runs still going; scheduled on the loop
    await asyncio.sleep(0)  # let the shutdown (and its cancellations) happen
    if running:
        await asyncio.gather(*running, return_exceptions=True)


@contextmanager
def _stop_on_signals(stop: asyncio.Event) -> Iterator[None]:
    """Set ``stop`` on SIGINT/SIGTERM while inside; restore the previous handlers on exit."""
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in STOP_SIGNALS}

    def request_stop(name: str) -> None:
        if not stop.is_set():
            logger.info("Received %s, shutting down", name)
        stop.set()

    def fallback_handler(signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(request_stop, signal.Signals(signum).name)

    for sig in STOP_SIGNALS:
        try:
            loop.add_signal_handler(sig, request_stop, sig.name)
        except NotImplementedError:  # Windows event loops have no add_signal_handler
            signal.signal(sig, fallback_handler)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            with suppress(NotImplementedError):
                loop.remove_signal_handler(sig)
            if handler is not None:
                signal.signal(sig, handler)
