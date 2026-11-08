"""Database setup: every table from the data model, WAL on, versioned migrations that run once."""

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ledgermind.db.migrate import (
    DEFAULT_DB_PATH,
    LATEST_VERSION,
    MigrationError,
    connect,
    migrate,
    open_database,
)

NOW = "2026-11-09T00:00:00+00:00"

# Every table with its columns and declared types. Money, quantities and timestamps are TEXT.
EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "tokens": {
        "id": "INTEGER",
        "binance_symbol": "TEXT",
        "base_asset": "TEXT",
        "chain": "TEXT",
        "contract_address": "TEXT",
        "decimals": "INTEGER",
        "active": "INTEGER",
        "updated_at": "TEXT",
    },
    "pools": {
        "id": "INTEGER",
        "token_id": "INTEGER",
        "chain": "TEXT",
        "pool_address": "TEXT",
        "dex": "TEXT",
        "liquidity_usd": "TEXT",
        "last_polled_at": "TEXT",
        "last_trade_cursor": "TEXT",
    },
    "onchain_trades": {
        "id": "INTEGER",
        "chain": "TEXT",
        "tx_hash": "TEXT",
        "pool_id": "INTEGER",
        "token_id": "INTEGER",
        "wallet": "TEXT",
        "side": "TEXT",
        "token_amount": "TEXT",
        "usd_value": "TEXT",
        "price_usd": "TEXT",
        "traded_at": "TEXT",
    },
    "wallets": {
        "id": "INTEGER",
        "chain": "TEXT",
        "address": "TEXT",
        "first_seen_at": "TEXT",
        "last_seen_at": "TEXT",
        "excluded": "INTEGER",
        "exclude_reason": "TEXT",
    },
    "wallet_scores": {
        "id": "INTEGER",
        "wallet_id": "INTEGER",
        "computed_at": "TEXT",
        "window_days": "INTEGER",
        "trade_count": "INTEGER",
        "win_rate": "REAL",
        "realized_pnl_usd": "TEXT",
        "unrealized_pnl_usd": "TEXT",
        "score": "REAL",
        "rank": "INTEGER",
    },
    "signals": {
        "id": "INTEGER",
        "created_at": "TEXT",
        "token_id": "INTEGER",
        "side": "TEXT",
        "strength": "INTEGER",
        "wallet_ids": "TEXT",
        "total_usd": "TEXT",
        "window_start_at": "TEXT",
        "window_end_at": "TEXT",
        "rule_params": "TEXT",
        "status": "TEXT",
        "skip_reason": "TEXT",
    },
    "orders": {
        "id": "INTEGER",
        "mode": "TEXT",
        "signal_id": "INTEGER",
        "binance_symbol": "TEXT",
        "side": "TEXT",
        "qty": "TEXT",
        "price_usdt": "TEXT",
        "quote_usdt": "TEXT",
        "fee": "TEXT",
        "fee_asset": "TEXT",
        "usdt_inr_rate": "TEXT",
        "inr_value": "TEXT",
        "reason_text": "TEXT",
        "exchange_order_id": "TEXT",
        "status": "TEXT",
        "created_at": "TEXT",
    },
    "positions": {
        "id": "INTEGER",
        "mode": "TEXT",
        "binance_symbol": "TEXT",
        "qty": "TEXT",
        "avg_cost_usdt": "TEXT",
        "opened_at": "TEXT",
        "closed_at": "TEXT",
        "realized_pnl_usdt": "TEXT",
        "open_signal_id": "INTEGER",
        "close_signal_id": "INTEGER",
    },
    "pot": {
        "mode": "TEXT",
        "starting_inr": "TEXT",
        "starting_usdt": "TEXT",
        "cash_usdt": "TEXT",
        "started_at": "TEXT",
    },
    "equity_snapshots": {
        "id": "INTEGER",
        "mode": "TEXT",
        "taken_at": "TEXT",
        "cash_usdt": "TEXT",
        "positions_value_usdt": "TEXT",
        "equity_usdt": "TEXT",
        "usdt_inr_rate": "TEXT",
    },
    "daily_reports": {
        "report_date": "TEXT",
        "window_start_at": "TEXT",
        "window_end_at": "TEXT",
        "notion_page_id": "TEXT",
        "status": "TEXT",
        "created_at": "TEXT",
    },
    "job_runs": {
        "id": "INTEGER",
        "job": "TEXT",
        "started_at": "TEXT",
        "finished_at": "TEXT",
        "ok": "INTEGER",
        "error_text": "TEXT",
        "items_processed": "INTEGER",
    },
}

# One valid row per table, in an order that satisfies the foreign keys (ids start at 1).
VALID_ROWS: dict[str, dict[str, Any]] = {
    "tokens": {
        "binance_symbol": "SOLUSDT",
        "base_asset": "SOL",
        "chain": "solana",
        "contract_address": "So11111111111111111111111111111111111111112",
        "decimals": 9,
        "active": 1,
        "updated_at": NOW,
    },
    "pools": {
        "token_id": 1,
        "chain": "solana",
        "pool_address": "pool-1",
        "dex": "raydium",
        "liquidity_usd": "250000.50",
        "last_polled_at": None,
        "last_trade_cursor": None,
    },
    "wallets": {
        "chain": "solana",
        "address": "wallet-1",
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "excluded": 0,
        "exclude_reason": None,
    },
    "onchain_trades": {
        "chain": "solana",
        "tx_hash": "tx-1",
        "pool_id": 1,
        "token_id": 1,
        "wallet": "wallet-1",
        "side": "buy",
        "token_amount": "10.5",
        "usd_value": "1500.00",
        "price_usd": "142.857",
        "traded_at": NOW,
    },
    "wallet_scores": {
        "wallet_id": 1,
        "computed_at": NOW,
        "window_days": 30,
        "trade_count": 12,
        "win_rate": 0.5,
        "realized_pnl_usd": "120.50",
        "unrealized_pnl_usd": "-3.10",
        "score": 0.8,
        "rank": 1,
    },
    "signals": {
        "created_at": NOW,
        "token_id": 1,
        "side": "buy",
        "strength": 3,
        "wallet_ids": "[1, 2, 3]",
        "total_usd": "4500.00",
        "window_start_at": NOW,
        "window_end_at": NOW,
        "rule_params": '{"min_wallets": 3}',
        "status": "new",
        "skip_reason": None,
    },
    "orders": {
        "mode": "paper",
        "signal_id": 1,
        "binance_symbol": "SOLUSDT",
        "side": "buy",
        "qty": "0.05",
        "price_usdt": "142.90",
        "quote_usdt": "7.145",
        "fee": "0.007145",
        "fee_asset": "USDT",
        "usdt_inr_rate": "88.50",
        "inr_value": "632.33",
        "reason_text": "3 top wallets bought SOL within 2 hours",
        "exchange_order_id": None,
        "status": "filled",
        "created_at": NOW,
    },
    "positions": {
        "mode": "paper",
        "binance_symbol": "SOLUSDT",
        "qty": "0.05",
        "avg_cost_usdt": "142.90",
        "opened_at": NOW,
        "closed_at": None,
        "realized_pnl_usdt": None,
        "open_signal_id": 1,
        "close_signal_id": None,
    },
    "pot": {
        "mode": "paper",
        "starting_inr": "2000",
        "starting_usdt": "22.60",
        "cash_usdt": "22.60",
        "started_at": NOW,
    },
    "equity_snapshots": {
        "mode": "paper",
        "taken_at": NOW,
        "cash_usdt": "15.40",
        "positions_value_usdt": "7.20",
        "equity_usdt": "22.60",
        "usdt_inr_rate": "88.50",
    },
    "daily_reports": {
        "report_date": "2026-11-09",
        "window_start_at": NOW,
        "window_end_at": NOW,
        "notion_page_id": "page-1",
        "status": "ok",
        "created_at": NOW,
    },
    "job_runs": {
        "job": "heartbeat",
        "started_at": NOW,
        "finished_at": NOW,
        "ok": 1,
        "error_text": None,
        "items_processed": 0,
    },
}

PARENT_TABLES = ("tokens", "pools", "wallets", "signals")


def insert(conn: sqlite3.Connection, table: str, **overrides: Any) -> None:
    row = {**VALID_ROWS[table], **overrides}
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row.values()))


def user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {name for (name,) in rows}


def schema_dump(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "ledgermind.db"


@pytest.fixture
def db(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_database(db_path)
    yield conn
    conn.close()


@pytest.fixture
def parents_db(db: sqlite3.Connection) -> sqlite3.Connection:
    """A migrated DB holding one token, pool, wallet and signal (all id 1)."""
    for table in PARENT_TABLES:
        insert(db, table)
    return db


# --- first run: file, tables, version, WAL ---------------------------------------------


def test_default_path_is_data_folder() -> None:
    assert Path("data/ledgermind.db") == DEFAULT_DB_PATH


def test_first_run_creates_file_and_missing_folder(db_path: Path) -> None:
    assert not db_path.parent.exists()

    open_database(db_path).close()

    assert db_path.is_file()


def test_first_run_creates_exactly_the_expected_tables(db: sqlite3.Connection) -> None:
    assert table_names(db) == set(EXPECTED_COLUMNS)


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_table_has_expected_columns_and_types(db: sqlite3.Connection, table: str) -> None:
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    columns = {name: declared_type for (_, name, declared_type, *_rest) in rows}

    assert columns == EXPECTED_COLUMNS[table]


def test_all_tables_are_strict(db: sqlite3.Connection) -> None:
    rows = db.execute(
        "SELECT name, strict FROM pragma_table_list WHERE schema = 'main' "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall()

    assert dict(rows) == dict.fromkeys(EXPECTED_COLUMNS, 1)


def test_first_run_sets_latest_version(db: sqlite3.Connection) -> None:
    assert LATEST_VERSION >= 1
    assert user_version(db) == LATEST_VERSION


def test_wal_mode_is_persisted_in_the_file(db_path: Path) -> None:
    open_database(db_path).close()

    plain = sqlite3.connect(db_path)
    try:
        assert plain.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        plain.close()


def test_connect_turns_on_foreign_keys(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_migrate_leaves_no_open_transaction(db: sqlite3.Connection) -> None:
    assert not db.in_transaction


def test_every_table_accepts_a_valid_row(db: sqlite3.Connection) -> None:
    for table in VALID_ROWS:
        insert(db, table)

    for table in VALID_ROWS:
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1


# --- second run: no-op -----------------------------------------------------------------


def test_second_run_is_a_noop(db_path: Path) -> None:
    first = open_database(db_path)
    insert(first, "tokens")
    schema_before = schema_dump(first)
    first.close()

    second = connect(db_path)
    try:
        result = migrate(second)

        assert result.before == LATEST_VERSION
        assert result.after == LATEST_VERSION
        assert result.applied == 0
        assert schema_dump(second) == schema_before
        assert second.execute("SELECT count(*) FROM tokens").fetchone()[0] == 1
    finally:
        second.close()


def test_first_run_reports_what_it_applied(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        result = migrate(conn)
    finally:
        conn.close()

    assert (result.before, result.after, result.applied) == (0, LATEST_VERSION, LATEST_VERSION)


def test_each_run_logs_one_line(db_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="ledgermind.db.migrate")

    open_database(db_path).close()
    open_database(db_path).close()

    records = [r for r in caplog.records if r.name == "ledgermind.db.migrate"]
    assert len(records) == 2
    assert all(f"version {LATEST_VERSION}" in r.getMessage() for r in records)


# --- versions: newer DB, upgrades, failures --------------------------------------------


def test_newer_database_is_refused_and_left_untouched(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True)
    plain = sqlite3.connect(db_path)
    plain.execute("CREATE TABLE future (x INTEGER)")
    plain.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    plain.commit()
    plain.close()

    with pytest.raises(MigrationError) as excinfo:
        open_database(db_path)

    message = str(excinfo.value)
    assert str(LATEST_VERSION + 1) in message
    assert str(LATEST_VERSION) in message
    check = sqlite3.connect(db_path)
    try:
        assert user_version(check) == LATEST_VERSION + 1
        assert table_names(check) == {"future"}
    finally:
        check.close()


STEP_1 = "CREATE TABLE a (x INTEGER) STRICT;"
STEP_2 = """
-- second step: a table and an index
CREATE TABLE b (y TEXT) STRICT;
CREATE INDEX b_y ON b (y);
"""


def test_upgrade_applies_only_the_missing_steps(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        migrate(conn, [STEP_1])
        conn.execute("INSERT INTO a VALUES (7)")

        result = migrate(conn, [STEP_1, STEP_2])

        assert (result.before, result.after, result.applied) == (1, 2, 1)
        assert user_version(conn) == 2
        assert table_names(conn) == {"a", "b"}
        assert conn.execute("SELECT x FROM a").fetchall() == [(7,)]
    finally:
        conn.close()


def test_failing_step_rolls_back_completely(db_path: Path) -> None:
    broken = "CREATE TABLE b (y TEXT);\nINSERT INTO does_not_exist VALUES (1);\n"
    conn = connect(db_path)
    try:
        migrate(conn, [STEP_1])

        with pytest.raises(MigrationError) as excinfo:
            migrate(conn, [STEP_1, broken])

        assert "2" in str(excinfo.value)
        assert user_version(conn) == 1
        assert table_names(conn) == {"a"}
        assert not conn.in_transaction
    finally:
        conn.close()


def test_failing_first_step_leaves_version_zero(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        with pytest.raises(MigrationError):
            migrate(conn, ["CREATE TABLE a (x INTEGER);\nCREATE TABLE a (x INTEGER);"])

        assert user_version(conn) == 0
        assert table_names(conn) == set()
    finally:
        conn.close()


def test_step_with_trigger_body_is_applied_whole(db_path: Path) -> None:
    step = """
CREATE TABLE a (x INTEGER);
CREATE TABLE log (x INTEGER);
CREATE TRIGGER a_log AFTER INSERT ON a BEGIN
    INSERT INTO log VALUES (new.x);
END;
"""
    conn = connect(db_path)
    try:
        migrate(conn, [step])
        conn.execute("INSERT INTO a VALUES (5)")

        assert conn.execute("SELECT x FROM log").fetchall() == [(5,)]
    finally:
        conn.close()


# --- constraints -----------------------------------------------------------------------


def test_duplicate_onchain_trade_is_rejected(parents_db: sqlite3.Connection) -> None:
    insert(parents_db, "onchain_trades")

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(parents_db, "onchain_trades")


def test_same_trade_other_side_is_allowed(parents_db: sqlite3.Connection) -> None:
    insert(parents_db, "onchain_trades")
    insert(parents_db, "onchain_trades", side="sell")


@pytest.mark.parametrize(
    ("table", "overrides"),
    [
        ("tokens", {"contract_address": "other"}),
        ("pools", {"pool_address": "other"}),
        ("wallets", {"address": "other"}),
    ],
)
def test_natural_keys_are_unique(
    parents_db: sqlite3.Connection, table: str, overrides: dict[str, Any]
) -> None:
    insert(parents_db, table, **overrides)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(parents_db, table, **overrides)


@pytest.mark.parametrize(
    ("table", "column", "bad_value", "extra"),
    [
        ("tokens", "active", 2, {"contract_address": "other"}),
        ("wallets", "excluded", 2, {"address": "other"}),
        ("onchain_trades", "side", "hold", {}),
        ("signals", "side", "short", {}),
        ("signals", "status", "done", {}),
        ("orders", "mode", "yolo", {}),
        ("orders", "side", "hold", {}),
        ("positions", "mode", "demo", {}),
        ("pot", "mode", "yolo", {}),
        ("equity_snapshots", "mode", "test", {}),
        ("daily_reports", "status", "failed", {}),
        ("job_runs", "ok", 2, {}),
    ],
)
def test_out_of_range_values_are_rejected(
    parents_db: sqlite3.Connection,
    table: str,
    column: str,
    bad_value: object,
    extra: dict[str, Any],
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(parents_db, table, **extra, **{column: bad_value})


def test_wrong_type_is_rejected(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        insert(db, "tokens", decimals="nine")


@pytest.mark.parametrize("column", ["usdt_inr_rate", "inr_value", "fee", "reason_text"])
def test_order_requires_tax_ledger_fields(parents_db: sqlite3.Connection, column: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        insert(parents_db, "orders", **{column: None})


def test_foreign_keys_are_enforced(parents_db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert(parents_db, "pools", pool_address="other", token_id=999)


@pytest.mark.parametrize("column", ["wallet_ids", "rule_params"])
def test_signal_json_columns_must_be_valid_json(
    parents_db: sqlite3.Connection, column: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(parents_db, "signals", **{column: "{not json"})


def test_only_one_open_position_per_mode_and_symbol(parents_db: sqlite3.Connection) -> None:
    insert(parents_db, "positions")

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(parents_db, "positions")


def test_open_position_allowed_after_close_or_in_other_mode(
    parents_db: sqlite3.Connection,
) -> None:
    insert(parents_db, "positions", closed_at=NOW)
    insert(parents_db, "positions")
    insert(parents_db, "positions", mode="live")

    assert parents_db.execute("SELECT count(*) FROM positions").fetchone()[0] == 3


def test_one_pot_per_mode(db: sqlite3.Connection) -> None:
    insert(db, "pot")
    insert(db, "pot", mode="live")

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(db, "pot")


def test_one_report_per_date(db: sqlite3.Connection) -> None:
    insert(db, "daily_reports")

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(db, "daily_reports")
