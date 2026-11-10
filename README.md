# LedgerMind

A personal, hands-off crypto trading bot. It finds profitable on-chain wallets from public DEX
trade data, buys on Binance spot when several of them agree, trades only from its own small pot
(paper mode first), and writes a plain-language daily report to Notion.

Personal experiment, not financial advice.

## Development

Requires [uv](https://docs.astral.sh/uv/). uv installs the Python version pinned in
`.python-version`.

```bash
uv sync                                          # install from uv.lock
cp config.example.toml config.toml              # your settings (never committed)
cp .env.example .env                             # your API keys, only when needed
uv run python -m ledgermind run                  # run the bot (Ctrl+C to stop)
uv run python -m ledgermind status               # mode, last heartbeat, last run per job
uv run pytest                                    # run the tests (offline)
uv run ruff check && uv run ruff format --check  # lint + format check
uv run ruff format                               # apply formatting
```

## Running it

To set up the machine that runs the bot (tools, keeping it awake, starting it in tmux, checking
on it over SSH), see [scripts/local_setup.md](scripts/local_setup.md).
