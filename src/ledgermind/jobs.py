"""Scheduled jobs and the boundary every job runs inside.

A job is an ``async`` function that does one chore and returns how many items it processed.
``run_recorded`` wraps each run: it writes a ``job_runs`` row when the run starts, closes it
with the outcome when it ends, and logs one line. A job that raises is recorded as failed and
never reaches the scheduler, so one broken job can't stop the others.
"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from ledgermind.db.repo import finish_job_run, start_job_run

logger = logging.getLogger(__name__)

Job = Callable[[], Awaitable[int]]
Clock = Callable[[], datetime]

CANCELLED_ERROR_TEXT = "cancelled at shutdown"
MAX_ERROR_TEXT_CHARS = 1000


def utc_now() -> datetime:
    return datetime.now(UTC)


async def run_recorded(conn: sqlite3.Connection, name: str, job: Job, now: Clock = utc_now) -> None:
    """Run ``job`` once, recording it in ``job_runs``. Only cancellation propagates."""
    run_id = _start(conn, name, now)
    began = time.monotonic()
    try:
        items = await job()
    except asyncio.CancelledError:
        _finish(conn, name, run_id, now, ok=False, error_text=CANCELLED_ERROR_TEXT)
        logger.warning("%s: cancelled at shutdown after %d ms", name, _elapsed_ms(began))
        raise
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_TEXT_CHARS]
        _finish(conn, name, run_id, now, ok=False, error_text=error_text)
        logger.exception("%s: failed after %d ms: %s", name, _elapsed_ms(began), error_text)
        return
    _finish(conn, name, run_id, now, ok=True, items_processed=items)
    logger.info("%s: ok, %d item(s) in %d ms", name, items, _elapsed_ms(began))


async def heartbeat() -> int:
    """Prove the scheduler is alive. The ``job_runs`` row is the heartbeat; nothing to process."""
    return 0


def _start(conn: sqlite3.Connection, name: str, now: Clock) -> int | None:
    try:
        return start_job_run(conn, name, now())
    except sqlite3.Error as exc:
        logger.error("%s: could not record the start in job_runs: %s", name, exc)
        return None


def _finish(
    conn: sqlite3.Connection,
    name: str,
    run_id: int | None,
    now: Clock,
    *,
    ok: bool,
    items_processed: int | None = None,
    error_text: str | None = None,
) -> None:
    if run_id is None:
        return
    try:
        finish_job_run(
            conn, run_id, now(), ok=ok, items_processed=items_processed, error_text=error_text
        )
    except (sqlite3.Error, LookupError) as exc:
        logger.error("%s: could not record the outcome in job_runs: %s", name, exc)


def _elapsed_ms(began: float) -> int:
    return round((time.monotonic() - began) * 1000)
