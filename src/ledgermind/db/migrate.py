"""Open the SQLite database and bring its schema up to date.

The schema is a list of migrations: a database at version N has had the first N migration files
applied, in order. The version lives in the file itself (``PRAGMA user_version``), so each start
applies only the steps the file hasn't seen yet, and a database that is already current is left
alone. Each step runs in one transaction together with its version bump, so a step that fails
leaves the database exactly as it was.

To change the schema, add a new numbered ``.sql`` file next to ``schema.sql``, append it to
``MIGRATION_FILES`` and never edit a file that has already been released. Note that
``PRAGMA foreign_keys`` can't be changed inside a migration (it is ignored in a transaction).
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/ledgermind.db")
BUSY_TIMEOUT_SECONDS = 5.0

# Migration files in this package, oldest first. Append only.
MIGRATION_FILES: tuple[str, ...] = ("schema.sql",)
LATEST_VERSION = len(MIGRATION_FILES)


class MigrationError(Exception):
    """The database can't be opened or brought up to date. The message is meant for the user."""


@dataclass(frozen=True)
class MigrationResult:
    """Schema version before and after a ``migrate`` call."""

    before: int
    after: int

    @property
    def applied(self) -> int:
        return self.after - self.before


def connect(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open ``path`` (creating its folder) with WAL mode, foreign keys on and manual transactions.

    Python never opens a transaction on its own on this connection (``isolation_level=None``):
    writes that must succeed or fail together go inside an explicit ``BEGIN`` ... ``COMMIT``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        journal_mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    except sqlite3.Error as exc:
        conn.close()
        raise MigrationError(f"{path}: cannot open database: {exc}") from None
    if journal_mode != "wal":
        conn.close()
        raise MigrationError(f"{path}: cannot switch to WAL mode (got {journal_mode!r})")
    return conn


def migrate(conn: sqlite3.Connection, migrations: Sequence[str] | None = None) -> MigrationResult:
    """Apply the migrations ``conn`` hasn't had yet; raise ``MigrationError`` if one fails.

    ``migrations`` (SQL scripts, oldest first) defaults to this package's migration files.
    """
    steps = list(migrations) if migrations is not None else load_migrations()
    latest = len(steps)
    before = _user_version(conn)
    _refuse_newer(conn, before, latest)
    for version in range(before + 1, latest + 1):
        _apply(conn, version, steps[version - 1], latest)
    after = _user_version(conn)
    logger.info(
        "Database %s at schema version %d (was %d, applied %d migration(s))",
        _file_name(conn),
        after,
        before,
        after - before,
    )
    return MigrationResult(before=before, after=after)


def open_database(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Connect to ``path`` and migrate it to the latest version."""
    conn = connect(path)
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def load_migrations() -> list[str]:
    """Read this package's migration files, oldest first."""
    package = files("ledgermind.db")
    return [package.joinpath(name).read_text(encoding="utf-8") for name in MIGRATION_FILES]


def _apply(conn: sqlite3.Connection, version: int, sql: str, latest: int) -> None:
    try:
        # IMMEDIATE takes the write lock now, so two processes starting together can't both
        # apply the same step: the second one waits, then sees the new version and skips.
        conn.execute("BEGIN IMMEDIATE")
        current = _user_version(conn)
        if current >= version:
            conn.execute("ROLLBACK")
            _refuse_newer(conn, current, latest)
            return
        for statement in _split_statements(sql):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    except (sqlite3.Error, sqlite3.Warning) as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(
            f"{_file_name(conn)}: migration {version} failed, database left at version "
            f"{_user_version(conn)}: {exc}"
        ) from None


def _split_statements(sql: str) -> list[str]:
    """Split a script into single statements, since ``execute`` runs only one at a time.

    (``executescript`` would be simpler, but on Python 3.11 it commits any open transaction
    first, which would break the one-transaction-per-step guarantee.)
    """
    statements: list[str] = []
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statements.append(pending)
            pending = ""
    if pending.strip():
        # Trailing comments run as a no-op; an unfinished statement makes SQLite raise.
        statements.append(pending)
    return statements


def _refuse_newer(conn: sqlite3.Connection, version: int, latest: int) -> None:
    if version > latest:
        raise MigrationError(
            f"{_file_name(conn)} is at schema version {version}, but this code only knows up to "
            f"version {latest}. It was probably written by a newer LedgerMind; update the code."
        )


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _file_name(conn: sqlite3.Connection) -> str:
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return file or ":memory:"
    return "database"
