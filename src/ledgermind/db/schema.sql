-- LedgerMind schema, version 1 (the first migration).
--
-- Never edit this file once a real database has been created from it. A schema change goes
-- in a new numbered migration file listed in migrate.py, which bumps PRAGMA user_version.
--
-- Conventions:
--   * Money and quantities are TEXT holding exact decimals (read back as decimal.Decimal).
--   * Timestamps are TEXT holding timezone-aware UTC ISO-8601 (e.g. 2026-11-09T11:30:00+00:00).
--   * Booleans are INTEGER 0/1.
--   * Tables are STRICT, so a value of the wrong type fails instead of being stored silently.

-- Binance spot USDT pairs we can trade and also watch on-chain.
CREATE TABLE tokens (
    id               INTEGER PRIMARY KEY,
    binance_symbol   TEXT    NOT NULL,                 -- e.g. SOLUSDT
    base_asset       TEXT    NOT NULL,                 -- e.g. SOL
    chain            TEXT    NOT NULL,                 -- GeckoTerminal chain id: solana, eth, bsc...
    contract_address TEXT    NOT NULL,
    decimals         INTEGER,
    active           INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    updated_at       TEXT    NOT NULL,
    UNIQUE (chain, contract_address)
) STRICT;

-- DEX pools we poll for trades, a few per token.
CREATE TABLE pools (
    id                INTEGER PRIMARY KEY,
    token_id          INTEGER NOT NULL REFERENCES tokens (id),
    chain             TEXT    NOT NULL,
    pool_address      TEXT    NOT NULL,
    dex               TEXT,
    liquidity_usd     TEXT,
    last_polled_at    TEXT,
    last_trade_cursor TEXT,
    UNIQUE (chain, pool_address)
) STRICT;

-- Large DEX trades, with the wallet (transaction signer) that made them.
CREATE TABLE onchain_trades (
    id           INTEGER PRIMARY KEY,
    chain        TEXT    NOT NULL,
    tx_hash      TEXT    NOT NULL,
    pool_id      INTEGER NOT NULL REFERENCES pools (id),
    token_id     INTEGER NOT NULL REFERENCES tokens (id),
    wallet       TEXT    NOT NULL,
    side         TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),  -- relative to our token
    token_amount TEXT    NOT NULL,
    usd_value    TEXT    NOT NULL,
    price_usd    TEXT    NOT NULL,
    traded_at    TEXT    NOT NULL,                     -- block time
    UNIQUE (chain, tx_hash, pool_id, wallet, side)     -- the same trade seen in two polls
) STRICT;

CREATE INDEX onchain_trades_traded_at ON onchain_trades (traded_at);

-- Every wallet seen trading, and whether the scorer ignores it (router, bot, exchange...).
CREATE TABLE wallets (
    id             INTEGER PRIMARY KEY,
    chain          TEXT    NOT NULL,
    address        TEXT    NOT NULL,
    first_seen_at  TEXT    NOT NULL,
    last_seen_at   TEXT    NOT NULL,
    excluded       INTEGER NOT NULL DEFAULT 0 CHECK (excluded IN (0, 1)),
    exclude_reason TEXT,
    UNIQUE (chain, address)
) STRICT;

-- One row per wallet per scoring run. win_rate and score are ratios, not money.
CREATE TABLE wallet_scores (
    id                 INTEGER PRIMARY KEY,
    wallet_id          INTEGER NOT NULL REFERENCES wallets (id),
    computed_at        TEXT    NOT NULL,
    window_days        INTEGER NOT NULL,
    trade_count        INTEGER NOT NULL,
    win_rate           REAL,                           -- NULL when no lot has closed yet
    realized_pnl_usd   TEXT    NOT NULL,
    unrealized_pnl_usd TEXT,                           -- NULL when no current price
    score              REAL    NOT NULL,
    rank               INTEGER
) STRICT;

CREATE INDEX wallet_scores_computed_at ON wallet_scores (computed_at, rank);

-- "Several top wallets agree": what the strategy decided and why.
CREATE TABLE signals (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT    NOT NULL,
    token_id        INTEGER NOT NULL REFERENCES tokens (id),
    side            TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),
    strength        INTEGER NOT NULL,                  -- how many wallets agreed
    wallet_ids      TEXT    NOT NULL CHECK (json_valid(wallet_ids)),   -- JSON array
    total_usd       TEXT,
    window_start_at TEXT,
    window_end_at   TEXT,
    rule_params     TEXT    NOT NULL CHECK (json_valid(rule_params)),  -- JSON object
    status          TEXT    NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'acted', 'skipped')),
    skip_reason     TEXT                               -- e.g. "below min order", "already held"
) STRICT;

-- Every buy and sell. This is the tax ledger and the report's "why".
CREATE TABLE orders (
    id                INTEGER PRIMARY KEY,
    mode              TEXT    NOT NULL CHECK (mode IN ('paper', 'live')),
    signal_id         INTEGER REFERENCES signals (id),
    binance_symbol    TEXT    NOT NULL,
    side              TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),
    qty               TEXT    NOT NULL,
    price_usdt        TEXT    NOT NULL,
    quote_usdt        TEXT    NOT NULL,
    fee               TEXT    NOT NULL,
    fee_asset         TEXT    NOT NULL,
    usdt_inr_rate     TEXT    NOT NULL,
    inr_value         TEXT    NOT NULL,
    reason_text       TEXT    NOT NULL,
    exchange_order_id TEXT,                            -- live mode only
    status            TEXT    NOT NULL,
    created_at        TEXT    NOT NULL
) STRICT;

-- Coins the bot holds (closed_at NULL) or held.
CREATE TABLE positions (
    id                INTEGER PRIMARY KEY,
    mode              TEXT    NOT NULL CHECK (mode IN ('paper', 'live')),
    binance_symbol    TEXT    NOT NULL,
    qty               TEXT    NOT NULL,
    avg_cost_usdt     TEXT    NOT NULL,
    opened_at         TEXT    NOT NULL,
    closed_at         TEXT,
    realized_pnl_usdt TEXT,
    open_signal_id    INTEGER REFERENCES signals (id),
    close_signal_id   INTEGER REFERENCES signals (id)
) STRICT;

-- At most one open position per coin in each mode.
CREATE UNIQUE INDEX positions_one_open
    ON positions (mode, binance_symbol) WHERE closed_at IS NULL;

-- The bot's own budget, one per mode, so paper history survives going live.
CREATE TABLE pot (
    mode          TEXT NOT NULL PRIMARY KEY CHECK (mode IN ('paper', 'live')),
    starting_inr  TEXT NOT NULL,
    starting_usdt TEXT NOT NULL,
    cash_usdt     TEXT NOT NULL,
    started_at    TEXT NOT NULL
) STRICT;

-- Cash plus holdings, every 15 minutes.
CREATE TABLE equity_snapshots (
    id                   INTEGER PRIMARY KEY,
    mode                 TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    taken_at             TEXT NOT NULL,
    cash_usdt            TEXT NOT NULL,
    positions_value_usdt TEXT NOT NULL,
    equity_usdt          TEXT NOT NULL,
    usdt_inr_rate        TEXT NOT NULL
) STRICT;

-- One row per day's report. The report itself is one row in a Notion database; this table only
-- remembers which Notion row belongs to which date and how writing it went, so a re-run updates
-- that row instead of adding a second one, and start-up can find missed days.
CREATE TABLE daily_reports (
    report_date     TEXT NOT NULL PRIMARY KEY CHECK (report_date = date(report_date)),  -- IST date
    window_start_at TEXT NOT NULL,
    window_end_at   TEXT NOT NULL,
    notion_page_id  TEXT,  -- ID of that day's Notion database row (the API calls a row a page)
    status          TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'caught_up')),
    created_at      TEXT NOT NULL
) STRICT;

-- One row per job run, for health checks and debugging.
CREATE TABLE job_runs (
    id              INTEGER PRIMARY KEY,
    job             TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,                              -- NULL while running
    ok              INTEGER CHECK (ok IN (0, 1)),      -- NULL while running
    error_text      TEXT,
    items_processed INTEGER
) STRICT;

CREATE INDEX job_runs_job_started_at ON job_runs (job, started_at);
